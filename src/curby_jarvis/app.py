"""CurbyJarvis — the orchestrator that wires the whole point-and-say controller.

This is the top-level join: it builds a CapabilityRouter registered with every
connector (cheapest-first chain), owns a single _Bridge (QObject pyqtSignals) so
background threads marshal confirm/cancel onto the Qt main thread, and stitches
together the deixis subsystem (PointerStream -> Calibration -> FusionBinder), the
LLM IntentParser, and the two overlay widgets (reticle + frosted card).

Pipeline for one utterance:

    utterance
      -> rule_table.lower()  (the <5ms hot path)  OR  IntentParser.parse()  (LLM)
      -> FusionBinder.bind()  (bind deixis to a screen point, or leave ambiguous)
      -> router.run(intent, confirm=overlay_confirm)

HEADLESS CONTRACT (HARD RULE): nothing at module import time touches PyQt6, pyobjc,
a socket, the camera, the network, or a permission. Every Qt / native / overlay
import is LAZY inside a method. `build_router()` and the entire --dry-run path run
under CI with no display, no mic, no camera, no permission, no API key — that path
is exactly what the golden harness drives.

`main()` exposes:
    --say "<utterance>"   one-shot through the full pipeline
    --dry-run             resolve + PREVIEW only (no execute); prints one JSON line
    --pointer X,Y         inject a fake bound deixis point (headless deixis demos)
    --live                full controller: voice in + gesture reticle + confirm
    --check               preflight: probe + request mic/Speech/AX, report status
"""
from __future__ import annotations

import json
import sys
from typing import Optional

# Support `python src/curby_jarvis/app.py ...` (run-as-script) in addition to the
# normal `-m curby_jarvis.app` / console-script entry. When __package__ is unset
# the relative imports below would fail, so we put src/ on the path and re-exec the
# module as a real package member. Pure stdlib — no headless violation.
if __package__ in (None, ""):  # pragma: no cover - exercised via the CLI smoke test
    import os
    _src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _src not in sys.path:
        sys.path.insert(0, _src)
    import runpy
    runpy.run_module("curby_jarvis.app", run_name="__main__", alter_sys=True)
    raise SystemExit(0)

from .intent import Intent, PreviewCard
from .router import CapabilityRouter


class CancellationToken:
    """Barge-in token (INF-12). A new utterance or an open-palm STOP gesture
    cancels the in-flight one; the router and task engine poll `.cancelled()`
    between atomic steps so a wrong/slow command can always be interrupted."""

    def __init__(self):
        import threading
        self._ev = threading.Event()

    def cancel(self) -> None:
        self._ev.set()

    def cancelled(self) -> bool:
        return self._ev.is_set()


class CurbyJarvis:
    """The controller. Build it, then `handle(utterance)` or drive `main()`.

    Construction is cheap and headless: connectors and the router are pure-Python
    objects; the pointer stream / overlay / Qt app are all lazily created and only
    when actually needed (live mode), never at import or in --dry-run.

    Args:
        vision:   pass-through to DeixisClickConnector (Tier-2 vision label on AX
                  miss). Off in headless/dry-run so we never reach for a camera/key.
        calibration: inject a calibration for tests; else lazily built on first use.
        stream:   inject a pointer stream for tests; else lazily built on first use.
        parser:   inject an IntentParser for tests; else lazily built on first use.
    """

    def __init__(
        self,
        *,
        vision: bool = False,
        calibration=None,
        stream=None,
        parser=None,
    ):
        self._vision = vision
        self._calibration = calibration
        self._stream = stream
        self._parser = parser

        self._router: Optional[CapabilityRouter] = None
        self._agent_loop = None      # AgentLoopConnector handle (built in build_router)
        self._fusion = None
        self._bridge = None          # Qt _Bridge — lazy (live mode only)
        self._app = None             # QApplication — lazy
        self._reticle = None         # ReticleWidget — lazy
        self._card = None            # PreviewCardWidget — lazy
        self._caption = None         # CaptionWidget — lazy (UI-03)
        self._edge = None            # EdgeLightWidget — lazy (UI-08)
        self._history = None         # HistoryWidget — lazy (UI-11)
        self._audio = None           # AudioFeedbackPlayer — lazy (INF-10/UI-13)
        self._phase_audio = None     # PhaseAudio — lazy
        self._voice = None           # stt.VoiceListener — lazy (live mode only)
        self._ret_timer = None       # QTimer driving the reticle — lazy
        self._session = None         # SessionState — lazy (INF-08 memory + undo ring)
        self._cancel = None          # CancellationToken for the in-flight utterance (INF-12)
        self._spec_intent = None     # cached speculative parse (INF-09)
        self._cur_phase = "idle"
        self._settle_gen = 0         # generation guard so a stale idle-timer can't fire mid-dispatch
        import threading
        self._confirm_lock = threading.Lock()  # one confirm card prompts at a time
        self._confirm_timeout_s = 30.0          # auto-Cancel a gated action if ignored
        self._dispatch_lock = threading.Lock()  # serialize the single bounded worker (INF-12)

    # -- router assembly ------------------------------------------------------

    def build_router(self) -> CapabilityRouter:
        """Build (once) the CapabilityRouter with every connector registered.

        Registration order is irrelevant to routing (the router sorts by cost then
        -confidence), but we list them cost-ascending for readability. Pure-Python:
        every connector defers its OS imports, so this runs headless.
        """
        if self._router is not None:
            return self._router

        # Lazy connector imports keep app import cheap; each connector module is
        # itself headless-safe (native imports live behind its methods).
        from .connectors.app_launch import AppLaunchConnector
        from .connectors.media_transport import MediaTransportConnector
        from .connectors.menu_command import MenuCommandConnector
        from .connectors.browser_tab import BrowserTabConnector
        from .connectors.deixis_click import DeixisClickConnector
        from .connectors.agent_fallback import AgentFallbackConnector

        connectors = [
            AppLaunchConnector(),                       # cost 1 — url/launch, zero TCC
            MediaTransportConnector(),                  # cost 2 — HID media keys
            MenuCommandConnector(),                     # cost 3 — frontmost menu bar
            DeixisClickConnector(vision=self._vision),  # cost 4 — AX press / click
            BrowserTabConnector(),                      # cost 6 — osascript tab control
            AgentFallbackConnector(),                   # cost 10 — open-ended agent floor
        ]
        self._router = CapabilityRouter(connectors)

        # -- revamp: widen the chain into "do anything via Claude" ------------
        # MCP adapters (cost 7): any configured MCP server becomes a tool the agent
        # loop can call. Returns [] when the SDK / ~/.curby/mcp_servers.json is absent.
        try:
            from .mcp_bridge import build_adapters
            for adapter in build_adapters():
                self._router.register(adapter)
        except Exception as e:  # MCP is optional — never block startup
            print(f"[curby-jarvis] mcp bridge skipped: {e}", file=sys.stderr)

        # Computer-use (cost 11): pixel-level vision fallback for AX-opaque surfaces.
        try:
            from .connectors.computer_use import ComputerUseConnector
            self._router.register(ComputerUseConnector())
        except Exception:
            pass

        # AgentLoopConnector (cost 9): THE open Claude tool-use loop. It composes
        # multi-step plans from the rest of the palette, so an utterance that maps to
        # no single verb still gets done. Wired AFTER the router so it can dispatch
        # back through the whole chain (deterministic fast path stays untouched).
        try:
            from .connectors.agent_loop import AgentLoopConnector
            router = self._router

            def _tools():
                # Expose every connector EXCEPT the open-ended agents themselves
                # (prevents the loop from recursing into itself / the CLI floor).
                return [c.tool_schema() for c in router.connectors
                        if c.name not in ("agent_loop", "agent_fallback")]

            self._agent_loop = AgentLoopConnector(
                dispatch=router.run, tools_provider=_tools,
                confirm=self._overlay_confirm)  # gate every sub-tool the loop runs
            self._router.register(self._agent_loop)
        except Exception as e:
            print(f"[curby-jarvis] agent loop unavailable: {e}", file=sys.stderr)
            self._agent_loop = None

        return self._router

    # -- deixis subsystem (lazy) ---------------------------------------------

    def _calib(self):
        if self._calibration is None:
            from .pointer.calibration import Calibration
            # load() degrades to an identity-into-primary fallback when no calib
            # file exists, so deixis still points roughly (confirm covers the slop).
            self._calibration = Calibration.load()
        return self._calibration

    def _pointer_stream(self):
        if self._stream is None:
            from .pointer.ws_client import PointerStream
            self._stream = PointerStream()
        return self._stream

    def _fusion_binder(self):
        if self._fusion is None:
            from .pointer.fusion import FusionBinder
            self._fusion = FusionBinder(self._pointer_stream(), self._calib())
        return self._fusion

    def intent_parser(self):
        if self._parser is None:
            from .connectors.intent_parse import IntentParser
            self._parser = IntentParser()
        return self._parser

    # -- the pipeline ---------------------------------------------------------

    def resolve(self, utterance: str, *, pointer=None, pointer2=None) -> Optional[Intent]:
        """Lower an utterance to an Intent, binding deixis.

        Path: rule_table.lower() (hot) -> IntentParser.parse() (cold LLM) -> None.
        A None all the way down is then handled by the caller as an agent_task so
        the chain still has somewhere to land.

        `pointer`/`pointer2` (logical px) are an explicit injection used by the
        --pointer demo flag and tests: when given, we bind them directly instead of
        consulting the live gesture stream, so deixis is testable fully headless.
        """
        from .rule_table import lower

        intent = lower(utterance)
        if intent is None:
            # Cold path: the LLM parser. Returns None with no API key / no SDK, in
            # which case the caller escalates to an agent_task.
            try:
                intent = self.intent_parser().parse(utterance)
            except Exception:
                intent = None
        if intent is None:
            return None

        intent = self._bind_pointer(intent, pointer=pointer, pointer2=pointer2)
        return intent

    def _bind_pointer(self, intent: Intent, *, pointer=None, pointer2=None) -> Intent:
        """Bind deixis: explicit injection wins; else the FusionBinder/gesture stream.

        For a non-deictic intent this is a no-op. For a deictic intent with an
        explicit `pointer`, we bind it directly (the --pointer / test path). With no
        explicit point we ask the FusionBinder, which returns the intent UNCHANGED
        if no fresh aimed sample exists -> pointer stays None -> risk == 'ambiguous'
        -> overlay forces 'point and confirm'. We NEVER synthesize a coordinate.
        """
        if not intent.needs_pointer:
            return intent
        if pointer is not None:
            return intent.bound_with(pointer=pointer, pointer2=pointer2)
        try:
            return self._fusion_binder().bind(intent)
        except Exception:
            # A wedged/absent stream degrades to no point -> ambiguous, not a crash.
            return intent

    def chosen_connector(self, intent: Intent, *, require_available: bool = True):
        """The connector the router would use for this intent.

        `require_available=True` mirrors CapabilityRouter.run() exactly: the cheapest
        candidate that both can_handle AND self-reports available (used by the live
        execute path). `require_available=False` (used by --dry-run) reports the
        INTENDED route — the cheapest candidate that can_handle, ignoring the local
        permission state. WHY: dry-run is an audit of where an utterance routes, and
        the menu/AX/deixis connectors declare themselves unavailable on a headless
        box with no Accessibility grant; that must not make every command look like
        an agent task. The golden harness asserts the intended route.
        """
        router = self.build_router()
        cands = router.candidates(intent)
        if require_available:
            for _conf, c in cands:
                if c.is_available(intent):
                    return c
        # Cheapest candidate that can_handle, regardless of availability.
        return cands[0][1] if cands else None

    # -- one-shot drive -------------------------------------------------------

    def handle(self, utterance: str, *, pointer=None, pointer2=None, confirm=None):
        """Resolve + route + EXECUTE one utterance. Returns a ConnectorResult.

        `confirm(card, intent) -> bool` gates irreversible/ambiguous actions; with
        None the router auto-runs only the safe ones and treats a gated action as
        cancelled-by-absence (it still previews + logs). For an unresolvable
        utterance we escalate to an agent_task so the chain always terminates.
        """
        intent = self.resolve(utterance, pointer=pointer, pointer2=pointer2)
        if intent is None:
            intent = self._agent_intent(utterance)
        router = self.build_router()
        return router.run(intent, confirm=confirm)

    def _agent_intent(self, utterance: str) -> Intent:
        """Wrap a fully-unresolved utterance as an explicit agent_task intent."""
        return Intent(verb="agent_task", target=utterance, raw_utterance=utterance)

    # -- dry-run (the golden-harness surface) ---------------------------------

    def dry_run(self, utterance: str, *, pointer=None, pointer2=None) -> dict:
        """Resolve + PREVIEW only (no execute). Returns the audit dict the CLI
        prints as one JSON line. Fully headless: no native call beyond what each
        connector's preview() lazily guards, and the golden tests rely on this.
        """
        intent = self.resolve(utterance, pointer=pointer, pointer2=pointer2)
        if intent is None:
            intent = self._agent_intent(utterance)

        connector = self.chosen_connector(intent, require_available=False)
        card: Optional[PreviewCard] = None
        mechanism = ""
        if connector is not None:
            try:
                card = connector.preview(intent)
            except Exception:
                card = None
            if card is not None:
                # Mirror the router: an unset card mechanism defaults to the name.
                card.mechanism = card.mechanism or connector.name
                mechanism = card.mechanism

        risk = card.risk if card is not None else intent.risk
        gloss = card.gloss if card is not None else ""
        literal = card.literal if card is not None else ""
        target_rect = list(card.target_rect) if (card and card.target_rect) else None

        return {
            "utterance": utterance,
            "verb": intent.verb,
            "needs_pointer": intent.needs_pointer,
            "chosen_connector": connector.name if connector is not None else None,
            "mechanism": mechanism,
            "risk": risk,
            "must_confirm": intent.must_confirm,
            "gloss": gloss,
            "literal": literal,
            "target_rect": target_rect,
        }

    # -- live mode (lazy Qt; voice in + gesture reticle + confirm gate) -------

    def run_live(self) -> int:
        """Bring up the full controller: Qt overlays, gesture stream → reticle,
        on-device voice → route → confirm → execute.

        This is the only path that touches Qt and the microphone, all lazily. The
        microphone listener is best-effort: if Speech access is denied or the
        framework is absent, the overlays + gesture reticle still run and the user
        can grant access and relaunch — a missing mic must not take down the
        controller. Each finished phrase is routed and executed on a worker thread
        so the audio/UI path always stays responsive.
        """
        from .prewarm import prewarm
        prewarm()  # warm the Anthropic TLS socket so the first LLM parse is snappy

        try:
            from PyQt6.QtWidgets import QApplication
        except Exception as e:  # no Qt -> can't run live; say so and bail cleanly
            print(f"[curby-jarvis] live mode needs PyQt6: {e}", file=sys.stderr)
            return 2

        self._app = QApplication.instance() or QApplication(sys.argv[:1])
        # macOS: an accessory app shows overlays without stealing focus / a Dock icon.
        try:
            from .macwin import set_accessory_policy
            set_accessory_policy()
        except Exception:
            pass

        self._ensure_overlay()
        self._bridge.phase.emit("idle")
        stream = self._pointer_stream()
        # Subscribe to named gestures BEFORE start() so no early frame is missed
        # (pinch-confirm, open-palm STOP, swipe verbs — INF-14 / UI-09).
        try:
            stream.gestures.subscribe(lambda k: self._bridge.gesture.emit(k))
        except Exception:
            pass
        stream.start()
        self._start_reticle_driver()
        self._start_stt()

        print("[curby-jarvis] live. Overlays up; routing voice → connectors. "
              "Ctrl-C to quit.", file=sys.stderr)
        try:
            return int(self._app.exec())
        finally:
            try:
                if self._voice is not None:
                    self._voice.stop()
            except Exception:
                pass
            try:
                self._pointer_stream().stop()
            except Exception:
                pass
            try:
                if self._ret_timer is not None:
                    self._ret_timer.stop()  # don't poll a stopped stream / dead widget
            except Exception:
                pass
            try:
                if self._session is not None:
                    self._session.close()  # flush + close the SQLite session store
            except Exception:
                pass

    def _ensure_overlay(self):
        """Lazily build the _Bridge + all HUD surfaces on the Qt main thread, and
        wire every revamp signal (phase, level, partial, lock, gesture, chain,
        ghost) to its widget. The cross-thread `invoke` marshal lets worker threads
        run a callable on the Qt main thread (overlay shows, card confirm).

        Each surface is guarded: a single widget that fails to build (e.g. a
        platform quirk) must not take down the reticle + card core."""
        if self._bridge is None:
            self._bridge = _make_bridge()
            # Queued (cross-thread) delivery: emitting `invoke` from any worker
            # thread runs the carried callable on the Qt main thread.
            self._bridge.invoke.connect(lambda fn: fn())
        if self._reticle is None:
            from .overlay.reticle import ReticleWidget
            self._reticle = ReticleWidget()
        if self._card is None:
            from .overlay.preview_card import PreviewCardWidget
            self._card = PreviewCardWidget()

        # -- ambient surfaces (best-effort) -----------------------------------
        if self._caption is None:
            try:
                from .overlay.caption import CaptionWidget
                self._caption = CaptionWidget()
            except Exception:
                self._caption = None
        if self._edge is None:
            try:
                from .overlay.edge_light import EdgeLightWidget
                self._edge = EdgeLightWidget()
            except Exception:
                self._edge = None
        if self._history is None:
            try:
                from .overlay.history import HistoryWidget
                self._history = HistoryWidget(on_undo=self._undo_action)
            except Exception:
                self._history = None

        # -- audio channel (INF-10 / UI-13) -----------------------------------
        if self._phase_audio is None:
            try:
                from .audio_feedback import AudioFeedbackPlayer
                from .overlay.audio_cue import PhaseAudio
                self._audio = AudioFeedbackPlayer()
                self._phase_audio = PhaseAudio(player=self._audio)
            except Exception:
                self._phase_audio = None

        self._wire_bridge()

    def _wire_bridge(self):
        """Connect every HUD signal to its surface. Connections run on the Qt main
        thread; each handler is itself defensive so a bad payload can't crash the UI."""
        b = self._bridge
        b.phase.connect(self._apply_phase)
        b.phase_meta.connect(self._apply_phase_meta)
        if self._caption is not None:
            b.partial.connect(self._caption.show_text)
        if self._reticle is not None:
            b.level.connect(self._reticle.set_level)
            b.lock.connect(self._reticle.set_lock_phase)
            b.confirm_progress.connect(self._reticle.set_confirm_progress)
            b.chain.connect(self._reticle.show_chain_progress)
            b.ghost_show.connect(self._reticle.show_ghost)
            b.ghost_move.connect(self._reticle.move_ghost)
            b.ghost_drop.connect(self._reticle.drop_ghost)
        b.gesture.connect(self._on_gesture)
        b.hide_all.connect(self._dismiss_all)

    # -- phase fan-out (UI-01) ------------------------------------------------

    def _apply_phase(self, p: str) -> None:
        """Single source-of-truth phase → every surface moves together."""
        self._cur_phase = p
        if self._reticle is not None:
            try: self._reticle.set_phase(p)
            except Exception: pass
        if self._edge is not None:
            try: self._edge.set_phase(p)
            except Exception: pass
        if self._caption is not None and p in ("heard", "done", "error"):
            try: self._caption.fade_out()
            except Exception: pass
        if self._phase_audio is not None:
            try: self._phase_audio.on_phase(p)
            except Exception: pass

    def _apply_phase_meta(self, meta) -> None:
        """Rich phase payload → the frosted card (status scaffold / final / latency)."""
        if self._card is None or meta is None:
            return
        try:
            ph = getattr(meta, "phase", "")
            if ph in ("understanding", "planning", "acting"):
                self._card.show_status(ph, getattr(meta, "text", ""))
            elif ph == "done" and getattr(meta, "latency", None):
                # UI-07: 'DID IT IN N ms' count-up chip on success.
                self._card.show_done(meta.latency)
        except Exception:
            pass

    def _emit_phase(self, p: str, *, text: str = "", mechanism: str = "",
                    risk: str = "", latency: Optional[dict] = None) -> None:
        """Emit a phase transition (+ meta) from any thread — marshaled by Qt."""
        if self._bridge is None:
            return
        from .overlay.phase import PhaseMeta
        try:
            self._bridge.phase.emit(p)
            self._bridge.phase_meta.emit(PhaseMeta(
                phase=p, text=text, mechanism=mechanism, risk=risk, latency=latency or {}))
        except Exception:
            pass

    def _run_on_main(self, fn) -> None:
        """Marshal a zero-arg callable onto the Qt main thread via the _Bridge."""
        self._bridge.invoke.emit(fn)

    # -- session memory + undo (INF-08 / INF-11) ------------------------------

    def session(self):
        """Lazy SessionState — cross-utterance memory + the undo ring. Degrades to
        None if the store can't be built (undo/history just go quiet)."""
        if self._session is None:
            try:
                from .session_state import SessionState
                self._session = SessionState()
            except Exception:
                self._session = None
        return self._session

    def _undo_action(self, undo_id=None) -> None:
        """Reverse the most recent reversible action (voice 'undo that', history
        Undo chip, or a future undo gesture). Pops the session undo ring."""
        sess = self.session()
        if sess is None:
            return
        try:
            popped = sess.pop_undo()
        except Exception:
            popped = None
        if not popped:
            return
        label, fn = popped
        ok = False
        try:
            ok = bool(fn())
        except Exception:
            ok = False
        self._emit_phase("done" if ok else "error",
                         text=(f"undid: {label}" if ok else f"couldn't undo: {label}"))
        self._settle_idle()

    # -- gesture handling (INF-14 / UI-09) ------------------------------------

    def _on_gesture(self, kind: str) -> None:
        """Map a recognized gesture to an action. Runs on the Qt main thread.
        pinch → confirm the open card; open_palm_stop → barge-in cancel;
        swipe → reversible fast-path verbs (auto-run)."""
        if kind == "pinch":
            self._confirm_via_pinch()
        elif kind == "open_palm_stop":
            self._cancel_inflight()
        elif kind in ("swipe_left", "swipe_right", "swipe_up"):
            verb = {"swipe_left": "previous", "swipe_right": "next", "swipe_up": "switch tab"}[kind]
            self._spawn_dispatch(verb)

    def _confirm_via_pinch(self) -> None:
        """If a confirm card is open, resolve it as Confirm (UI-09). The pinch-fill
        arc itself is driven by the gesture stream surfacing confirm_progress."""
        pend = getattr(self, "_pending_confirm", None)
        if pend is not None:
            self._safe_call(self._bridge.confirm_progress.emit, 1.0)  # snap arc to full
            pend["ok"] = True
            pend["ev"].set()

    def _cancel_inflight(self) -> None:
        """Open-palm STOP: cancel the in-flight utterance + any open confirm."""
        if self._cancel is not None:
            self._cancel.cancel()
        pend = getattr(self, "_pending_confirm", None)
        if pend is not None:
            pend["ok"] = False
            pend["ev"].set()
        if self._audio is not None:
            try: self._audio.play_error()
            except Exception: pass

    def _dismiss_all(self) -> None:
        try:
            if self._card is not None: self._card.dismiss()
        except Exception: pass
        try:
            if self._caption is not None: self._caption.fade_out()
        except Exception: pass
        try:
            if self._reticle is not None: self._reticle.drop_ghost()
        except Exception: pass

    def _settle_idle(self, delay_ms: int = 1400) -> None:
        """Return the HUD to idle a beat after a terminal phase, so DONE/ERROR are
        seen before the orb quiets. The QTimer is created ON THE MAIN THREAD (via
        _run_on_main) — a singleShot scheduled from the worker thread has no event
        loop and silently never fires. Guarded by a generation counter so a stale
        timer from a prior dispatch can't fire 'idle' over an active next one."""
        try:
            from PyQt6.QtCore import QTimer
            self._settle_gen += 1
            gen = self._settle_gen

            def _arm():
                QTimer.singleShot(
                    delay_ms,
                    lambda: self._bridge.phase.emit("idle") if self._settle_gen == gen else None,
                )
            self._run_on_main(_arm)
        except Exception:
            pass

    # -- gesture → reticle ----------------------------------------------------

    def _start_reticle_driver(self) -> None:
        """Poll the pointer stream at ~30fps and drive the crosshair reticle.

        The reticle follows the freshest aimed/confident fingertip sample mapped
        to logical screen pixels; it hides when no qualifying sample is present
        (no hand, low confidence, or the hand-signal daemon is down)."""
        from PyQt6.QtCore import QTimer

        self._ret_timer = QTimer()
        self._ret_timer.timeout.connect(self._poll_pointer)
        self._ret_timer.start(33)

    def _poll_pointer(self) -> None:
        try:
            sample = self._pointer_stream().latest()
        except Exception:
            sample = None
        if sample is None:
            if self._reticle is not None and self._reticle.isVisible():
                self._reticle.hide()
            return
        try:
            x, y = self._calib().map(sample.x_norm, sample.y_norm)
            self._reticle.show_reticle(x, y)
            if self._caption is not None:
                self._caption.set_pos(x, y + 40)  # caption tracks under the crosshair
        except Exception:
            pass

    # -- voice → route → confirm → execute ------------------------------------

    def _start_stt(self) -> None:
        """Start the on-device voice listener; route each finished phrase."""
        try:
            from .stt import VoiceListener
        except Exception as e:
            print(f"[curby-jarvis] STT module unavailable: {e}", file=sys.stderr)
            return
        import os
        # The rule-based fast-endpoint: a high-confidence command like 'pause' or
        # 'next tab' finalizes the moment it's recognized instead of waiting the
        # full silence window (INF-07) — the biggest perceived-latency win.
        try:
            from .rule_table import fast_match
        except Exception:
            fast_match = None
        # Optional wake word. Defaults to "hey curby" so ambient speech isn't
        # routed; override/disable via CURBY_WAKE (set CURBY_WAKE="" to turn off).
        wake = os.environ.get("CURBY_WAKE", "hey curby") or None
        self._voice = VoiceListener(
            self._on_voice_utterance,
            wake_word=wake,
            on_status=lambda m: print(f"[curby-jarvis] {m}", file=sys.stderr),
            on_partial=self._on_partial,                       # UI-03 live caption + INF-09
            on_level=lambda lvl: self._bridge.level.emit(lvl),  # UI-04 audio-reactive orb
            fast_endpoint_check=fast_match,                     # INF-07 early endpoint
        )
        if not self._voice.start():
            print("[curby-jarvis] voice input off (grant Microphone + Speech "
                  "Recognition, then relaunch). Gesture + overlays still live.",
                  file=sys.stderr)
        else:
            self._bridge.phase.emit("listening")

    def _on_partial(self, text: str) -> None:
        """STT partial (watcher thread): stream the running transcript to the caption
        and speculatively parse once the phrase is long enough (INF-09) so the cold
        parse is often already done by the time speech ends."""
        text = (text or "").strip()
        if not text:
            return
        try:
            self._bridge.partial.emit(text)
        except Exception:
            pass
        # One cheap speculative parse per phrase, once it's substantial. Run it on
        # a daemon thread — speculative_parse is a ~700ms HTTP call and _on_partial
        # runs on the Speech framework's delivery queue; blocking it here would
        # delay every subsequent partial AND the isFinal endpoint callback.
        if not getattr(self, "_spec_done", False) and len(text.split()) >= 6:
            self._spec_done = True
            _text = text

            def _run_spec():
                try:
                    self._spec_intent = self.intent_parser().speculative_parse(_text)
                except Exception:
                    self._spec_intent = None
            import threading
            threading.Thread(target=_run_spec, name="curby-spec", daemon=True).start()

    def _on_voice_utterance(self, text: str) -> None:
        """Voice callback (watcher thread): acknowledge, barge-in over any in-flight
        command, and hand off to the single bounded worker so neither the audio path
        nor the Qt loop ever blocks on routing/execution."""
        text = (text or "").strip()
        if not text:
            return
        print(f"[curby-jarvis] heard: {text!r}", file=sys.stderr)
        self._spec_done = False                      # arm speculative parse for the next phrase
        self._emit_phase("heard", text=text)         # instant ack (chime + caption settle)
        self._spawn_dispatch(text)

    def _spawn_dispatch(self, text: str) -> None:
        """Barge-in: cancel the in-flight utterance, then start a fresh cancellable
        worker for this one (INF-12). The old worker sees its token flip and bails
        between atomic steps instead of stacking unbounded threads.

        Held under _dispatch_lock because two threads call this — the STT watcher
        (_on_voice_utterance) and the Qt main thread (_on_gesture swipe verbs) —
        and they must not race the cancel/assign of self._cancel."""
        import threading
        with self._dispatch_lock:
            if self._cancel is not None:
                self._cancel.cancel()
            # Resolve any open confirm card so a worker blocked in _overlay_confirm's
            # ev.wait() exits immediately instead of lingering for the 30s timeout —
            # makes barge-in feel instant and prevents two live dispatch workers.
            pend = getattr(self, "_pending_confirm", None)
            if pend is not None:
                pend["ok"] = False
                pend["ev"].set()
            token = CancellationToken()
            self._cancel = token
            threading.Thread(
                target=self._dispatch_utterance, args=(text, token),
                name="curby-dispatch", daemon=True,
            ).start()

    def _dispatch_utterance(self, text: str, token: "CancellationToken") -> None:
        """Worker thread: narrate phases, resolve, route + execute with the live
        confirm gate, then record + offer undo. Cancellable at every boundary."""
        import time
        t0 = time.time()
        # Snapshot + clear the speculative-parse cache up front (single assignment
        # is atomic under the GIL) so a barge-in worker can't read a stale spec.
        spec_intent = self._spec_intent
        self._spec_intent = None
        try:
            self._emit_phase("understanding", text=text)
            intent = self._resolve_live(text, spec_intent=spec_intent)
            t_resolved = time.time()
            if token.cancelled():
                return

            # Reset the radial chain ring, then narrate routing.
            if self._reticle is not None:
                self._run_on_main(lambda: self._safe_call(self._reticle.reset_chain))
            self._emit_phase("planning", text=intent.verb, mechanism="")

            router = self.build_router()
            if intent.verb == "agent_task" and self._agent_loop is not None:
                res = self._run_agentic(intent, token)
            else:
                self._emit_phase("acting", text=f"{intent.verb} {intent.target}".strip())
                res = router.run(
                    intent, confirm=self._overlay_confirm,
                    on_chain=lambda name, ok: self._bridge.chain.emit(name, ok),
                    on_event=self._on_progress, cancel_token=token,
                )
            t_done = time.time()
        except Exception as e:  # never let a bad utterance kill the listener
            print(f"[curby-jarvis] dispatch error: {e!r}", file=sys.stderr)
            self._emit_phase("error", text=str(e)[:80])
            self._settle_idle()
            return

        if token.cancelled() or res.error == "cancelled":
            return  # barge-in / palm-stop: stay quiet, the next phrase owns the HUD

        # Latency breakdown for the 'did it in N ms' chip (UI-07 / INF-06).
        latency = {
            "parse_ms": round((t_resolved - t0) * 1000.0, 1),
            "exec_ms": round((t_done - t_resolved) * 1000.0, 1),
            "total_ms": round((t_done - t0) * 1000.0, 1),
        }
        self._record_and_offer_undo(intent, res, latency)

        tag = "ok" if res.ok else (res.error or "no-op")
        print(f"[curby-jarvis] {tag} ({res.mechanism}) {latency['total_ms']}ms", file=sys.stderr)
        if res.ok:
            self._emit_phase("done", text=(res.detail_text or "done"),
                             mechanism=res.mechanism, latency=latency)
            self._speak(res.detail_text)
        else:
            self._emit_phase("error", text=(res.error or "no-op"), mechanism=res.mechanism)
        self._settle_idle()

    def _run_agentic(self, intent: Intent, token) -> "object":
        """Drive the open-ended path through the multi-step task engine (INF-08):
        per-step progress + cancellation + session ledger around the agent loop."""
        _on_chain = lambda name, ok: self._bridge.chain.emit(name, ok)
        router = self.build_router()
        try:
            from .task_engine import TaskRunner
            runner = TaskRunner(
                # Wrap router.run so the chain ring + progress + cancel reach the HUD
                # for agent-task steps too (not just the single-verb path).
                dispatch=lambda i, confirm=None: router.run(
                    i, confirm=confirm, on_chain=_on_chain,
                    on_event=self._on_progress, cancel_token=token),
                confirm=self._overlay_confirm,
                on_progress=self._on_progress,
                cancel_token=token,
                session=self.session(),
            )
            return runner.run_agentic(intent, self._agent_loop)
        except Exception:
            # Fall back to a direct streamed agent-loop run if the engine is absent.
            return router.run(
                intent, confirm=self._overlay_confirm, on_chain=_on_chain,
                on_event=self._on_progress, cancel_token=token)

    def _on_progress(self, ev) -> None:
        """A ProgressEvent from a streaming connector (agent loop / computer use):
        narrate it on the card + speak completed sentences (INF-10)."""
        try:
            text = getattr(ev, "text", "") or ""
            ph = getattr(ev, "phase", "acting") or "acting"
            if text:
                self._emit_phase(ph if ph in ("planning", "acting") else "acting",
                                 text=text, mechanism=getattr(ev, "mechanism", ""))
            if getattr(ev, "kind", "") == "token":
                self._speak_stream(text)
        except Exception:
            pass

    def _record_and_offer_undo(self, intent: Intent, res, latency: dict) -> None:
        """Persist the action to session memory and, when reversible, push it onto
        the undo ring + show the post-action undo toast (INF-08 / INF-11 / UI-11)."""
        sess = self.session()
        if sess is None:
            return
        label = f"{intent.verb} {intent.target}".strip() or intent.verb
        undo_id = None
        if res.ok and getattr(res, "undo_fn", None) is not None:
            try:
                undo_id = sess.push_undo(label, res.undo_fn)
                if self._card is not None:
                    self._run_on_main(
                        lambda: self._safe_call(self._card.show_undo_toast, label, 5,
                                                self._undo_action))
            except Exception:
                undo_id = None
        try:
            sess.record_action(intent.verb, intent.target, res.mechanism, bool(res.ok),
                               risk=intent.risk, undo_label=(label if undo_id else None))
        except Exception:
            pass
        # Surface undo_id into the telemetry JSONL so the history overlay (UI-11)
        # can render an Undo chip on this row.
        if undo_id:
            try:
                from .telemetry import emit as _tel_emit
                _tel_emit(surface="operational", verb=intent.verb, target=intent.target,
                          mechanism=res.mechanism, ok=True, risk=intent.risk, undo_id=undo_id)
            except Exception:
                pass

    # -- TTS readback (INF-10) ------------------------------------------------

    def _speak(self, text) -> None:
        """Speak a final reply sentence-streamed (on-device, off the audio loop)."""
        text = (text or "").strip()
        if not text or self._audio is None:
            return
        try:
            from .audio_feedback import speak_sentence
            import threading
            threading.Thread(target=speak_sentence, args=(text,), daemon=True).start()
        except Exception:
            pass

    def _speak_stream(self, delta) -> None:
        """Feed a streaming token delta to the sentence aggregator for early TTS."""
        agg = getattr(self, "_sentence_agg", None)
        if agg is None:
            try:
                from .audio_feedback import SentenceAggregator, speak_sentence
                agg = SentenceAggregator(speak=speak_sentence)
                self._sentence_agg = agg
            except Exception:
                return
        try:
            agg.feed(delta)
        except Exception:
            pass

    @staticmethod
    def _safe_call(fn, *a) -> None:
        try:
            fn(*a)
        except Exception:
            pass

    def _resolve_live(self, text: str, spec_intent=None) -> Intent:
        """Live resolve: filler-normalize, rule-table fast path, then the cached
        speculative parse (INF-09) before a cold parse, then bind deixis. Kept
        separate from resolve()/handle() so the golden/dry-run contract is untouched.
        spec_intent is the snapshot the caller cleared, avoiding a shared-state read."""
        from .rule_table import lower
        try:
            from .nlp_utils import normalize_transcript
            norm = normalize_transcript(text) or text
        except Exception:
            norm = text
        intent = lower(norm)
        if intent is None:
            spec = spec_intent
            first = norm.lower().split()[0] if norm.split() else ""
            if (spec is not None and spec.verb
                    and (spec.raw_utterance or "").lower().startswith(first)):
                intent = spec  # speculative parse already finished while the user spoke
            else:
                try:
                    intent = self.intent_parser().parse(norm)
                except Exception:
                    intent = None
        if intent is None:
            return self._agent_intent(text)
        return self._bind_pointer(intent)

    # -- live confirm marshaling ----------------------------------------------

    def _overlay_confirm(self, card: PreviewCard, intent: Intent) -> bool:
        """Synchronous confirm gate for live mode — shows the frosted card and
        blocks the calling worker thread on the user's Confirm/Cancel, marshaled
        onto the Qt main thread through the _Bridge.

        Serialized by `_confirm_lock` so two phrases can't race the same card.
        Defaults to Cancel on timeout, so an irreversible action never fires
        without an explicit human Confirm.
        """
        import threading

        with self._confirm_lock:
            ev = threading.Event()
            pend = {"ev": ev, "ok": False}
            # Expose the pending confirm so a pinch gesture (UI-09) or open-palm
            # STOP (INF-12) can resolve it without a mouse.
            self._pending_confirm = pend

            def show():  # runs on the Qt main thread
                self._safe_call(self._bridge.confirm_progress.emit, 0.0)  # reset pinch arc
                def on_confirm():
                    pend["ok"] = True
                    ev.set()

                def on_cancel():
                    pend["ok"] = False
                    ev.set()

                self._card.show_card(card, on_confirm=on_confirm, on_cancel=on_cancel)

            self._run_on_main(show)
            try:
                if not ev.wait(timeout=self._confirm_timeout_s):
                    self._run_on_main(self._card.dismiss)
                    return False
                return pend["ok"]
            finally:
                self._pending_confirm = None
                self._run_on_main(lambda: self._safe_call(self._bridge.confirm_progress.emit, 0.0))


# ---- _Bridge (QObject pyqtSignals) — built lazily, never at import ----------

def _make_bridge():
    """Construct the single _Bridge QObject the app owns for thread->Qt marshaling.

    Defined as a factory (not a module-level class) so PyQt6 is imported ONLY here,
    honoring the headless contract. Widgets never touch threads; background workers
    (gesture stream, STT) emit on these signals and Qt delivers them on the main
    thread where the overlays live.
    """
    from PyQt6.QtCore import QObject, pyqtSignal

    class _Bridge(QObject):
        # (x, y) logical px — move the reticle under the fingertip.
        reticle = pyqtSignal(float, float)
        # (risk,) — recolor the reticle/bracket.
        risk = pyqtSignal(str)
        # a resolved utterance is ready to preview+route (raw utterance string).
        utterance = pyqtSignal(str)
        # hide all overlays.
        hide_all = pyqtSignal()
        # run a zero-arg callable on the Qt main thread (worker → UI marshal).
        invoke = pyqtSignal(object)
        # ---- revamp HUD signals (UI-01 phase hub + sibling surfaces) ----
        phase = pyqtSignal(str)            # SessionPhase transition → every surface
        phase_meta = pyqtSignal(object)    # PhaseMeta payload (text/mechanism/latency)
        partial = pyqtSignal(str)          # live STT partial transcript (UI-03)
        level = pyqtSignal(float)          # 0..1 mic RMS amplitude (UI-04)
        gesture = pyqtSignal(str)          # named gesture kind (INF-14)
        confirm_progress = pyqtSignal(float)  # pinch-to-confirm arc 0..1 (UI-09)
        chain = pyqtSignal(str, bool)      # connector-chain walk (name, resolved) (UI-15)
        lock = pyqtSignal(float)           # targeting bracket lock 0..1 (UI-05)
        ghost_show = pyqtSignal(object)    # (x,y,w,h) start a grab-move ghost (UI-10)
        ghost_move = pyqtSignal(float, float)
        ghost_drop = pyqtSignal()

    return _Bridge()


# ---- CLI --------------------------------------------------------------------

def _parse_pointer(spec: Optional[str]):
    """Parse a '--pointer X,Y' spec into (x, y) floats, or None."""
    if not spec:
        return None
    try:
        xs, ys = spec.split(",", 1)
        return (float(xs.strip()), float(ys.strip()))
    except (ValueError, AttributeError):
        return None


def build_arg_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="curby-jarvis",
        description="Voice + hand-gesture universal computer controller — point and say.",
    )
    p.add_argument("--say", metavar="UTTERANCE",
                   help="run one utterance through the full pipeline")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve + preview only (no execute); prints one JSON line")
    p.add_argument("--pointer", metavar="X,Y",
                   help="inject a fake bound deixis point (logical px) for headless demos")
    p.add_argument("--pointer2", metavar="X,Y",
                   help="inject the destination point for a two-point move/drag")
    p.add_argument("--vision", action="store_true",
                   help="enable Tier-2 vision labeling on an AX miss (needs key + capture)")
    p.add_argument("--live", action="store_true",
                   help="run the full controller: voice in + gesture reticle + confirm")
    p.add_argument("--check", action="store_true",
                   help="preflight: probe + request mic/Speech/Accessibility, report status")
    return p


def _open_settings_pane(anchor: str) -> None:
    """Open a Privacy & Security settings pane (best-effort, darwin only)."""
    if sys.platform != "darwin":
        return
    try:
        import subprocess
        subprocess.run(
            ["open", f"x-apple.systempreferences:com.apple.preference.security?{anchor}"],
            check=False,
        )
    except Exception:
        pass


def run_check() -> int:
    """Preflight the live controller's capabilities and (re)request permissions.

    Probes the Speech framework, requests Microphone + Speech Recognition access
    (showing the OS prompts), checks the Accessibility grant the AX connectors
    need, the `claude` agent floor, and the gesture websocket. Prints a human
    report and opens the relevant Settings pane for anything denied. Returns 0
    when voice input is usable (mic + speech authorized), else 2.
    """
    from . import stt

    print("curby-jarvis preflight")
    print("======================")

    if not stt.speech_framework_available():
        print("  speech framework : UNAVAILABLE (pip install "
              "pyobjc-framework-Speech pyobjc-framework-AVFoundation)")
        return 2

    summary = stt.request_authorizations(timeout=30.0)
    if "error" in summary:
        print(f"  speech framework : ERROR — {summary['error']}")
        return 2

    speech_ok = summary.get("speech") == "authorized"
    mic_ok = summary.get("mic") == "authorized"
    print(f"  microphone       : {summary.get('mic')}")
    print(f"  speech recognition: {summary.get('speech')}"
          f"  (on-device: {'yes' if summary.get('on_device') else 'no'})")

    # Accessibility — needed by the menu/deixis connectors (not voice itself).
    try:
        from .ax.ax_bridge import ax_available
        ax_ok = bool(ax_available())
    except Exception:
        ax_ok = False
    print(f"  accessibility    : {'trusted' if ax_ok else 'NOT trusted'}")

    # Agent floor.
    import shutil
    claude = shutil.which("claude")
    print(f"  agent floor      : {'claude @ ' + claude if claude else 'claude NOT on PATH'}")

    # Gesture websocket (optional — deixis only).
    import importlib.util
    if importlib.util.find_spec("websockets") is None:
        ws = "websockets dep missing"
    else:
        ws = _probe_pointer_ws()
    print(f"  gesture ws       : {ws}")

    # Extended capability tiers (INF-13): Automation + Screen Recording TCC —
    # so --check no longer reports all-green while browser_tab / computer-use are dead.
    try:
        from . import permissions as _perm
        rep = _perm.full_report()
        autom = rep.get("automation", "unknown")
        screen_rec = bool(rep.get("screen_recording", False))
        print(f"  automation (AppleScript): {autom}")
        print(f"  screen recording : {'granted' if screen_rec else 'NOT granted (vision fallback off)'}")
        if autom == "denied":
            _open_settings_pane("Privacy_Automation")
        if not screen_rec:
            try:
                _perm.request_screen_recording()
            except Exception:
                pass
            _open_settings_pane("Privacy_ScreenCapture")
    except Exception as e:
        print(f"  extended probes  : unavailable ({e})")

    # MCP servers (optional generality — the agent loop's external tool palette).
    try:
        from .mcp_bridge import load_config
        cfg = load_config()
        print(f"  mcp servers      : {len(cfg)} configured" if cfg
              else "  mcp servers      : none (add ~/.curby/mcp_servers.json)")
    except Exception:
        pass

    # Guide the user to any pane that needs a toggle.
    if not mic_ok:
        _open_settings_pane("Privacy_Microphone")
    if not speech_ok:
        _open_settings_pane("Privacy_SpeechRecognition")
    if not ax_ok:
        _open_settings_pane("Privacy_Accessibility")

    print()
    if speech_ok and mic_ok:
        print("  → voice input READY. Run:  curby-jarvis --live")
        if not ax_ok:
            print("    (grant Accessibility too for menu/click commands.)")
        return 0
    print("  → grant the permissions above (panes opened), then rerun --check.")
    return 2


def _probe_pointer_ws(timeout: float = 0.6) -> str:
    """Best-effort one-shot connect to the hand-signal pointer websocket."""
    import asyncio

    async def _try() -> str:
        import websockets
        from .pointer.ws_client import DEFAULT_URL
        try:
            async with websockets.connect(DEFAULT_URL, open_timeout=timeout):
                return f"reachable @ {DEFAULT_URL}"
        except Exception:
            return f"down @ {DEFAULT_URL} (start hand-signal for pointer/deixis)"

    try:
        return asyncio.run(_try())
    except Exception:
        return "probe failed"


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    pointer = _parse_pointer(args.pointer)
    pointer2 = _parse_pointer(args.pointer2)

    if args.check:
        return run_check()

    jarvis = CurbyJarvis(vision=bool(args.vision))

    if args.live:
        return jarvis.run_live()

    if args.say is not None:
        if args.dry_run:
            audit = jarvis.dry_run(args.say, pointer=pointer, pointer2=pointer2)
            print(json.dumps(audit))
            return 0
        # Real one-shot: confirm=None auto-runs only the safe actions; gated ones
        # are previewed + logged but not auto-fired without a human.
        res = jarvis.handle(args.say, pointer=pointer, pointer2=pointer2)
        print(json.dumps({"ok": res.ok, "mechanism": res.mechanism,
                          "error": res.error, "detail": res.detail}))
        return 0 if res.ok else 1

    build_arg_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

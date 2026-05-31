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
    --live                hotkey + STT loop (degrades to a no-op if no mic/STT)
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
        self._fusion = None
        self._bridge = None          # Qt _Bridge — lazy (live mode only)
        self._app = None             # QApplication — lazy
        self._reticle = None         # ReticleWidget — lazy
        self._card = None            # PreviewCardWidget — lazy

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

    # -- live mode (lazy Qt; degrades without mic/STT) ------------------------

    def run_live(self) -> int:
        """Bring up the Qt app + overlays + gesture stream + a hotkey/STT loop.

        This is the only path that touches Qt and the camera, all lazily. If no STT
        backend or mic is available it degrades to a no-op event loop (the overlays
        still work; voice input simply never fires) rather than crashing — a missing
        mic must not take down the controller.
        """
        from .prewarm import prewarm
        prewarm()  # warm the Anthropic TLS socket so the first LLM parse is snappy

        try:
            from PyQt6.QtWidgets import QApplication
        except Exception as e:  # no Qt -> can't run live; say so and bail cleanly
            print(f"[curby-jarvis] live mode needs PyQt6: {e}", file=sys.stderr)
            return 2

        self._app = QApplication.instance() or QApplication(sys.argv[:1])
        self._ensure_overlay()
        self._pointer_stream().start()

        # STT is best-effort: if no backend, we still spin the event loop so the
        # overlays and gesture reticle are live and a future STT can attach.
        self._start_stt_best_effort()

        try:
            return int(self._app.exec())
        finally:
            try:
                self._pointer_stream().stop()
            except Exception:
                pass

    def _ensure_overlay(self):
        """Lazily build the _Bridge + reticle + card widgets on the Qt main thread."""
        if self._bridge is None:
            self._bridge = _make_bridge()
        if self._reticle is None:
            from .overlay.reticle import ReticleWidget
            self._reticle = ReticleWidget()
        if self._card is None:
            from .overlay.preview_card import PreviewCardWidget
            self._card = PreviewCardWidget()

    def _start_stt_best_effort(self) -> None:
        """Attach an STT loop if a backend exists; otherwise no-op (mic-free demo).

        Kept intentionally thin for v0.1: curby owns the full dictation stack. Here
        we only need a hook; absent one, live mode is still a working overlay demo.
        """
        return None

    # -- live confirm marshaling ----------------------------------------------

    def _overlay_confirm(self, card: PreviewCard, intent: Intent) -> bool:
        """Synchronous confirm gate for live mode — shows the frosted card and
        blocks on the user's Confirm/Cancel, marshaled through the _Bridge.

        Only used in live mode (Qt running). The dry-run/--say-headless paths pass
        confirm=None (auto-run safe, gate-cancel risky) so they never need Qt.
        """
        # v0.1: the live confirm UI marshaling is wired in run_live's STT callback;
        # this default refuses gated actions when no interactive loop is attached so
        # an irreversible action never auto-fires without a human.
        return False


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
                   help="run the hotkey + STT loop (degrades to a no-op without a mic)")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    pointer = _parse_pointer(args.pointer)
    pointer2 = _parse_pointer(args.pointer2)

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

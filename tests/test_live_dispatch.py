"""Headless tests for the live-mode wiring in app.py.

Live mode itself needs Qt + a mic, but its *control flow* — the worker→main
marshal, the confirm gate's confirm/cancel/timeout outcomes, the pointer→reticle
poll, and the voice→dispatch handoff — is pure orchestration. Here we drive it
with fakes (no QApplication, no Speech framework, no pointer socket), exactly the
way the rest of the suite stays headless-green.
"""
from __future__ import annotations

import threading

from curby_jarvis.app import CurbyJarvis
from curby_jarvis.intent import ConnectorResult, Intent, PreviewCard


# ---- fakes ------------------------------------------------------------------

class _FakeSignal:
    """Stand-in for pyqtSignal(object): emit() runs the callable synchronously,
    modeling immediate delivery on the Qt main thread."""

    def emit(self, fn):
        fn()


class _NoopSignal:
    """Stand-in for a typed pyqtSignal: records emits, ignores connects."""

    def __init__(self):
        self.calls = []

    def emit(self, *a):
        self.calls.append(a)

    def connect(self, *a, **k):
        pass


class _FakeBridge:
    def __init__(self):
        self.invoke = _FakeSignal()
        # The revamp HUD signals — no-op recorders so dispatch/phase code runs headless.
        for name in ("phase", "phase_meta", "partial", "level", "gesture",
                     "confirm_progress", "chain", "lock", "ghost_show",
                     "ghost_move", "ghost_drop", "reticle", "risk", "utterance", "hide_all"):
            setattr(self, name, _NoopSignal())


class _FakeCard:
    """Records show_card calls and simulates a user's button choice."""

    def __init__(self, action="confirm"):
        self.action = action          # "confirm" | "cancel" | "ignore"
        self.shown = []
        self.dismissed = 0

    def show_card(self, card, on_confirm=None, on_cancel=None, auto_dismiss_ms=None):
        self.shown.append(card)
        if self.action == "confirm" and on_confirm:
            on_confirm()
        elif self.action == "cancel" and on_cancel:
            on_cancel()
        # "ignore" fires nothing — models a user who never answers.

    def dismiss(self):
        self.dismissed += 1


class _FakeSample:
    x_norm = 0.5
    y_norm = 0.5


class _FakeStream:
    def __init__(self, sample):
        self._s = sample

    def latest(self, *a, **k):
        return self._s


class _FakeCalib:
    def map(self, nx, ny):
        return (nx * 100.0, ny * 200.0)


class _FakeReticle:
    def __init__(self):
        self._visible = False
        self.shown = []
        self.hidden = 0

    def isVisible(self):  # noqa: N802 - mirrors Qt name
        return self._visible

    def show_reticle(self, x, y):
        self._visible = True
        self.shown.append((x, y))

    def hide(self):
        self._visible = False
        self.hidden += 1


# ---- confirm gate -----------------------------------------------------------

def test_overlay_confirm_true_on_confirm():
    j = CurbyJarvis()
    j._bridge = _FakeBridge()
    j._card = _FakeCard("confirm")
    ok = j._overlay_confirm(PreviewCard(title="close", risk="irreversible"),
                            Intent(verb="close"))
    assert ok is True
    assert j._card.shown and j._card.dismissed == 0


def test_overlay_confirm_false_on_cancel():
    j = CurbyJarvis()
    j._bridge = _FakeBridge()
    j._card = _FakeCard("cancel")
    assert j._overlay_confirm(PreviewCard(title="x"), Intent(verb="close")) is False


def test_overlay_confirm_times_out_to_cancel():
    j = CurbyJarvis()
    j._bridge = _FakeBridge()
    j._card = _FakeCard("ignore")
    j._confirm_timeout_s = 0.05
    assert j._overlay_confirm(PreviewCard(title="x"), Intent(verb="close")) is False
    assert j._card.dismissed == 1  # an ignored card is dismissed, action cancelled


# ---- pointer → reticle ------------------------------------------------------

def test_poll_pointer_shows_reticle_for_sample():
    j = CurbyJarvis()
    j._stream = _FakeStream(_FakeSample())
    j._calibration = _FakeCalib()
    j._reticle = _FakeReticle()
    j._poll_pointer()
    assert j._reticle.shown == [(50.0, 100.0)]


def test_poll_pointer_hides_when_no_sample():
    j = CurbyJarvis()
    j._stream = _FakeStream(None)
    j._reticle = _FakeReticle()
    j._reticle._visible = True
    j._poll_pointer()
    assert j._reticle.hidden == 1


# ---- voice → dispatch -------------------------------------------------------

def test_dispatch_routes_through_router_with_confirm_gate():
    """New barge-in dispatch: resolve → router.run with the live confirm gate AND
    the in-flight cancellation token threaded through."""
    from curby_jarvis.app import CancellationToken

    j = CurbyJarvis()
    j._bridge = _FakeBridge()
    j.session = lambda: None  # skip the real SQLite store in this unit test
    seen = {}

    class _FakeRouter:
        def run(self, intent, confirm=None, on_chain=None, on_event=None, cancel_token=None):
            seen["confirm"] = confirm
            seen["intent"] = intent
            seen["cancel"] = cancel_token
            return ConnectorResult(ok=True, mechanism="media_key")

    j.build_router = lambda: _FakeRouter()
    j._resolve_live = lambda text: Intent(verb="mute", raw_utterance=text)

    tok = CancellationToken()
    j._dispatch_utterance("mute", tok)
    assert seen["intent"].verb == "mute"
    assert seen["confirm"] == j._overlay_confirm  # live gate is wired in
    assert seen["cancel"] is tok                   # barge-in token threaded through


def test_dispatch_swallows_handler_exceptions():
    from curby_jarvis.app import CancellationToken

    j = CurbyJarvis()
    j._bridge = _FakeBridge()
    j.session = lambda: None

    def boom(text):
        raise RuntimeError("kaboom")

    j._resolve_live = boom
    j._dispatch_utterance("anything", CancellationToken())  # must not raise — listener stays alive


def test_on_voice_utterance_spawns_dispatch():
    j = CurbyJarvis()
    j._bridge = _FakeBridge()
    done = threading.Event()
    got = {}
    j._dispatch_utterance = lambda text, token: (got.__setitem__("text", text), done.set())
    j._on_voice_utterance("open spotify")
    assert done.wait(timeout=2.0)
    assert got["text"] == "open spotify"


def test_spawn_dispatch_cancels_previous_inflight():
    """A new utterance must cancel the prior in-flight token (INF-12 barge-in)."""
    j = CurbyJarvis()
    j._bridge = _FakeBridge()
    started = threading.Event()
    j._dispatch_utterance = lambda text, token: started.set()
    j._spawn_dispatch("first")
    assert started.wait(timeout=2.0)
    first_token = j._cancel
    started.clear()
    j._spawn_dispatch("second")
    assert started.wait(timeout=2.0)
    assert first_token.cancelled()       # the first was cancelled when the second arrived
    assert j._cancel is not first_token   # a fresh token owns the new utterance


def test_on_voice_utterance_ignores_empty():
    j = CurbyJarvis()
    j._bridge = _FakeBridge()
    n = {"calls": 0}
    j._dispatch_utterance = lambda t, tok: n.__setitem__("calls", n["calls"] + 1)
    j._on_voice_utterance("   ")
    assert n["calls"] == 0


def test_run_on_main_marshals_via_bridge():
    j = CurbyJarvis()
    j._bridge = _FakeBridge()
    ran = {"x": False}
    j._run_on_main(lambda: ran.__setitem__("x", True))
    assert ran["x"] is True

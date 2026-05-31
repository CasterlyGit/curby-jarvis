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


class _FakeBridge:
    def __init__(self):
        self.invoke = _FakeSignal()


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

def test_dispatch_routes_through_handle_with_confirm_gate():
    j = CurbyJarvis()
    seen = {}

    def fake_handle(text, confirm=None):
        seen["text"] = text
        seen["confirm"] = confirm
        return ConnectorResult(ok=True, mechanism="media_key")

    j.handle = fake_handle
    j._dispatch_utterance("mute")
    assert seen["text"] == "mute"
    assert seen["confirm"] == j._overlay_confirm  # live gate is wired in


def test_dispatch_swallows_handler_exceptions():
    j = CurbyJarvis()

    def boom(text, confirm=None):
        raise RuntimeError("kaboom")

    j.handle = boom
    j._dispatch_utterance("anything")  # must not raise — listener stays alive


def test_on_voice_utterance_spawns_dispatch():
    j = CurbyJarvis()
    done = threading.Event()
    got = {}
    j._dispatch_utterance = lambda text: (got.__setitem__("text", text), done.set())
    j._on_voice_utterance("open spotify")
    assert done.wait(timeout=2.0)
    assert got["text"] == "open spotify"


def test_on_voice_utterance_ignores_empty():
    j = CurbyJarvis()
    n = {"calls": 0}
    j._dispatch_utterance = lambda t: n.__setitem__("calls", n["calls"] + 1)
    j._on_voice_utterance("   ")
    assert n["calls"] == 0


def test_run_on_main_marshals_via_bridge():
    j = CurbyJarvis()
    j._bridge = _FakeBridge()
    ran = {"x": False}
    j._run_on_main(lambda: ran.__setitem__("x", True))
    assert ran["x"] is True

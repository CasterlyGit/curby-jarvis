"""Headless tests for GestureBus, ws_client gesture frame parsing, and
fusion.gesture_confirm().

No display, no socket, no Qt, no camera — all dependencies are fakes or
injectable clocks.
"""
from __future__ import annotations

import importlib
import time
from typing import List

import pytest

from curby_jarvis.pointer.gesture_bus import GestureBus, KINDS, HYSTERESIS_FRAMES, COOLDOWN_S
from curby_jarvis.pointer.ws_client import PointerSample, PointerStream
from curby_jarvis.pointer.fusion import FusionBinder
from curby_jarvis.intent import Intent


# ============================================================================
# GestureBus — hysteresis + cooldown
# ============================================================================

class _FakeClock:
    """Injectable monotonic clock whose time is fully controlled by tests."""

    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _make_bus(hysteresis: int = HYSTERESIS_FRAMES, cooldown_s: float = COOLDOWN_S,
              clock: _FakeClock = None) -> tuple[GestureBus, _FakeClock, List[str]]:
    """Convenience: create a bus with injected clock + a recording subscriber."""
    if clock is None:
        clock = _FakeClock(start=1.0)
    fired: List[str] = []
    bus = GestureBus(hysteresis=hysteresis, cooldown_s=cooldown_s, clock=clock)
    bus.subscribe(fired.append)
    return bus, clock, fired


def test_module_imports_headless():
    m = importlib.import_module("curby_jarvis.pointer.gesture_bus")
    assert hasattr(m, "GestureBus")
    assert hasattr(m, "KINDS")


# -- hysteresis: must see >=HYSTERESIS_FRAMES frames before firing -----------

def test_hysteresis_fires_after_threshold():
    bus, clock, fired = _make_bus(hysteresis=3)
    for _ in range(3):
        bus.feed("pinch", conf=0.9)
    assert fired == ["pinch"]


def test_hysteresis_does_not_fire_before_threshold():
    bus, clock, fired = _make_bus(hysteresis=3)
    bus.feed("pinch", conf=0.9)
    bus.feed("pinch", conf=0.9)
    assert fired == []


def test_broken_run_resets_count():
    """An interrupting kind breaks the run; must re-accumulate from scratch."""
    bus, clock, fired = _make_bus(hysteresis=3)
    bus.feed("pinch")
    bus.feed("pinch")
    bus.feed("fist")      # breaks the run
    bus.feed("pinch")
    bus.feed("pinch")
    assert fired == []    # only 2 consecutive pinches after the reset


def test_broken_run_then_completes():
    bus, clock, fired = _make_bus(hysteresis=3)
    bus.feed("pinch")
    bus.feed("fist")
    bus.feed("pinch")
    bus.feed("pinch")
    bus.feed("pinch")
    assert fired == ["pinch"]


# -- cooldown: same kind suppressed within cooldown window -------------------

def test_cooldown_suppresses_immediate_repeat():
    bus, clock, fired = _make_bus(hysteresis=3, cooldown_s=0.5)
    # Fire once.
    for _ in range(3):
        bus.feed("pinch")
    assert fired == ["pinch"]
    fired.clear()
    # Re-accumulate within cooldown window — no second fire.
    for _ in range(3):
        bus.feed("pinch")
    assert fired == []


def test_cooldown_allows_after_window():
    bus, clock, fired = _make_bus(hysteresis=3, cooldown_s=0.5)
    for _ in range(3):
        bus.feed("pinch")
    assert fired == ["pinch"]
    fired.clear()
    clock.advance(0.6)   # past cooldown
    for _ in range(3):
        bus.feed("pinch")
    assert fired == ["pinch"]


def test_cooldown_does_not_suppress_different_kind():
    """After a pinch fires, a different gesture kind should still be gatable."""
    bus, clock, fired = _make_bus(hysteresis=1, cooldown_s=0.5)
    bus.feed("pinch")
    assert "pinch" in fired
    fired.clear()
    bus.feed("fist")
    assert "fist" in fired   # different kind — not suppressed


# -- edge trigger: exactly ONE event at the transition -----------------------

def test_single_fire_not_per_frame():
    """Holding a pose for many frames fires exactly once (per cooldown window)."""
    bus, clock, fired = _make_bus(hysteresis=3, cooldown_s=0.5)
    for _ in range(30):    # 30 consecutive pinch frames (~1s at 30fps)
        bus.feed("pinch")
    assert fired.count("pinch") == 1


def test_second_fire_after_cooldown_and_accumulation():
    bus, clock, fired = _make_bus(hysteresis=3, cooldown_s=0.5)
    for _ in range(3):
        bus.feed("pinch")
    clock.advance(0.6)
    for _ in range(3):
        bus.feed("pinch")
    assert fired.count("pinch") == 2


# -- subscribe / unsubscribe -------------------------------------------------

def test_unsubscribe_stops_delivery():
    bus, clock, fired = _make_bus(hysteresis=1)
    bus.unsubscribe(fired.append)
    bus.feed("pinch")
    assert fired == []


def test_unsubscribe_unknown_fn_is_silent():
    bus = GestureBus()
    bus.unsubscribe(lambda k: None)   # never registered — must not raise


def test_subscribe_same_fn_once():
    fired: List[str] = []
    bus = GestureBus(hysteresis=1, clock=_FakeClock(1.0))
    bus.subscribe(fired.append)
    bus.subscribe(fired.append)   # double-subscribe should not double-deliver
    bus.feed("pinch")
    assert fired == ["pinch"]


# -- subscriber exception does not wedge bus ---------------------------------

def test_bad_subscriber_does_not_wedge():
    def boom(k):
        raise RuntimeError("oops")

    fired: List[str] = []
    clock = _FakeClock(1.0)
    bus = GestureBus(hysteresis=1, clock=clock)
    bus.subscribe(boom)
    bus.subscribe(fired.append)
    bus.feed("pinch")
    assert fired == ["pinch"]


# -- KINDS constant ----------------------------------------------------------

def test_kinds_contains_expected():
    assert "pinch" in KINDS
    assert "open_palm_stop" in KINDS
    assert "fist" in KINDS
    assert "swipe_left" in KINDS
    assert "swipe_right" in KINDS
    assert "swipe_up" in KINDS
    assert "point" in KINDS
    assert "thumbs_up" in KINDS


# ============================================================================
# ws_client — gesture frame parsing
# ============================================================================

def test_gesture_frame_feeds_bus():
    """A {t:gesture,kind:pinch} frame should reach the GestureBus."""
    ps = PointerStream()
    clock = _FakeClock(1.0)
    # Inject a pre-built bus with a known clock so we can control the state.
    from curby_jarvis.pointer.gesture_bus import GestureBus
    bus = GestureBus(hysteresis=1, clock=clock)
    fired: List[str] = []
    bus.subscribe(fired.append)
    # Force the stream to use our pre-built bus (via the lazy property slot).
    with ps._gesture_lock:
        ps._gestures = bus

    ps._ingest({"t": "gesture", "kind": "pinch", "conf": 0.92, "ts": time.time()})
    assert "pinch" in fired


def test_gesture_frame_returns_none():
    """A gesture frame never produces a PointerSample."""
    ps = PointerStream()
    result = ps._ingest({"t": "gesture", "kind": "fist", "conf": 0.8})
    assert result is None


def test_gesture_frame_missing_kind_does_not_raise():
    ps = PointerStream()
    result = ps._ingest({"t": "gesture", "conf": 0.9})   # no kind key
    assert result is None


def test_legacy_gesture_frame_still_ignored():
    """Legacy {gesture:...} frames must not become samples and must not raise."""
    ps = PointerStream()
    result = ps._ingest({"gesture": "tick", "ts": time.time()})
    assert result is None
    assert ps.latest() is None


def test_pointer_frame_with_gesture_kind_field():
    """Pointer frames that carry an inline gesture_kind tag should expose it."""
    ps = PointerStream()
    frame = {
        "t": "pointer", "v": 2,
        "ts": time.time(), "present": True,
        "x": 0.5, "y": 0.5,
        "conf": 0.95, "aim_ok": True,
        "mirrored": False, "src_w": 640, "src_h": 480,
        "gesture_kind": "point",
    }
    s = ps._ingest(frame)
    assert s is not None
    assert s.gesture_kind == "point"


def test_pointer_frame_without_gesture_kind_is_none():
    ps = PointerStream()
    frame = {
        "t": "pointer", "v": 2,
        "ts": time.time(), "present": True,
        "x": 0.3, "y": 0.7,
        "conf": 0.90, "aim_ok": True,
        "mirrored": True, "src_w": 640, "src_h": 480,
    }
    s = ps._ingest(frame)
    assert s is not None
    assert s.gesture_kind is None


def test_stream_gestures_property_lazy():
    """Accessing .gestures should always return a GestureBus."""
    from curby_jarvis.pointer.gesture_bus import GestureBus
    ps = PointerStream()
    assert isinstance(ps.gestures, GestureBus)
    # Second access returns the same instance.
    assert ps.gestures is ps.gestures


# ============================================================================
# fusion.gesture_confirm()
# ============================================================================

class _FakeCalib:
    W, H = 1440, 900

    def map(self, nx, ny):
        return (nx * self.W, ny * self.H)


class _FakeStream:
    """Minimal stream fake; .gestures is a real GestureBus with injectable clock."""

    def __init__(self, bus: GestureBus):
        self.gestures = bus

    def latest(self, **kwargs):
        return None   # fusion.bind always gets no sample


def test_gesture_confirm_true_after_pinch():
    clock = _FakeClock(10.0)
    bus = GestureBus(hysteresis=1, clock=clock)
    stream = _FakeStream(bus)
    binder = FusionBinder(stream, _FakeCalib())

    bus.feed("pinch")
    assert binder.gesture_confirm(within_s=1.2) is True


def test_gesture_confirm_false_before_any_gesture():
    clock = _FakeClock(10.0)
    bus = GestureBus(hysteresis=1, clock=clock)
    stream = _FakeStream(bus)
    binder = FusionBinder(stream, _FakeCalib())

    assert binder.gesture_confirm() is False


def test_gesture_confirm_false_after_window_expires():
    clock = _FakeClock(10.0)
    bus = GestureBus(hysteresis=1, clock=clock)
    stream = _FakeStream(bus)
    binder = FusionBinder(stream, _FakeCalib())

    bus.feed("pinch")
    clock.advance(2.0)   # past within_s=1.2
    assert binder.gesture_confirm(within_s=1.2) is False


def test_gesture_confirm_false_for_non_pinch():
    """Only 'pinch' counts as a confirmation gesture."""
    clock = _FakeClock(10.0)
    bus = GestureBus(hysteresis=1, clock=clock)
    stream = _FakeStream(bus)
    binder = FusionBinder(stream, _FakeCalib())

    bus.feed("fist")
    assert binder.gesture_confirm(within_s=1.2) is False


def test_gesture_confirm_false_when_no_gestures_attr():
    """Stream without .gestures degrades gracefully."""
    class _NoGestureStream:
        def latest(self, **kwargs):
            return None

    binder = FusionBinder(_NoGestureStream(), _FakeCalib())
    assert binder.gesture_confirm() is False


def test_gesture_confirm_false_on_exception():
    """Any internal error must return False, never raise."""
    class _BrokenStream:
        @property
        def gestures(self):
            raise RuntimeError("bus dead")

        def latest(self, **kwargs):
            return None

    binder = FusionBinder(_BrokenStream(), _FakeCalib())
    assert binder.gesture_confirm() is False

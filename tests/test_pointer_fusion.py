"""Headless tests for pointer.fusion.FusionBinder.

No display, no camera, no socket, no permissions: the stream is a scripted fake
and the calibration is a deterministic affine fake (we avoid the real
Calibration.map identity path because it does a lazy Qt geometry lookup, which
isn't headless-safe). One test also exercises the REAL PointerSample dataclass
to keep the duck-typed contract honest.
"""
from __future__ import annotations

import time

import pytest

from curby_jarvis.intent import Intent, RISK_AMBIGUOUS, RISK_REVERSIBLE
from curby_jarvis.pointer.fusion import FusionBinder
from curby_jarvis.pointer.ws_client import PointerSample


# -- fakes -------------------------------------------------------------------

class _FakeSample:
    """Minimal duck of PointerSample: fusion only reads x_norm/y_norm."""

    def __init__(self, x_norm, y_norm):
        self.x_norm = x_norm
        self.y_norm = y_norm


class _FakeStream:
    """Returns a scripted list of samples on successive latest() calls.

    Each latest() pops the next scripted value; when exhausted returns the last
    (so a single-script stream behaves like a held-still hand). Records the
    kwargs of the most recent call so we can assert the gates passed through.
    """

    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.last_kwargs = None

    def latest(self, **kwargs):
        self.last_kwargs = kwargs
        if not self._script:
            return None
        if self._i < len(self._script):
            v = self._script[self._i]
            self._i += 1
        else:
            v = self._script[-1]
        return v


class _RaisingStream:
    """latest() blows up — fusion must swallow it and treat as 'no sample'."""

    def latest(self, **kwargs):
        raise RuntimeError("ws wedged")


class _FakeCalib:
    """Deterministic affine: scale normalized [0,1] onto a 1440x900 logical screen."""

    W, H = 1440, 900

    def map(self, nx, ny):
        return (nx * self.W, ny * self.H)


class _RaisingCalib:
    def map(self, nx, ny):
        raise ValueError("singular matrix")


# -- helpers -----------------------------------------------------------------

def _deictic_click() -> Intent:
    return Intent("click_at", needs_pointer=True, raw_utterance="click this")


def _deictic_move() -> Intent:
    # mirrors rule_table's 'move THIS THERE' lowering
    return Intent("move", needs_pointer=True, reversible=False,
                  args={"two_point": True}, raw_utterance="move this there")


# -- tests -------------------------------------------------------------------

def test_fresh_aimed_sample_binds_mapped_coord():
    stream = _FakeStream([_FakeSample(0.5, 0.5)])
    binder = FusionBinder(stream, _FakeCalib())

    out = binder.bind(_deictic_click(), word_ts=time.time())

    assert out.pointer == (720.0, 450.0)          # 0.5*1440, 0.5*900
    assert out.needs_pointer is True
    # resolved deixis on a reversible verb -> no longer ambiguous
    assert out.risk == RISK_REVERSIBLE
    assert out.must_confirm is False


def test_gates_forwarded_to_stream():
    """fusion must ask the stream for an aimed, confident, fresh sample."""
    stream = _FakeStream([_FakeSample(0.1, 0.2)])
    FusionBinder(stream, _FakeCalib(), freshness_ms=800).bind(_deictic_click())

    kw = stream.last_kwargs
    assert kw["require_aim"] is True
    assert kw["min_conf"] == pytest.approx(0.6)
    assert kw["max_age_s"] == pytest.approx(0.8)   # 800ms -> 0.8s


def test_no_sample_leaves_pointer_none_and_ambiguous():
    stream = _FakeStream([None])
    out = FusionBinder(stream, _FakeCalib()).bind(_deictic_click())

    assert out.pointer is None
    assert out.risk == RISK_AMBIGUOUS              # forces 'point and confirm'
    assert out.must_confirm is True


def test_non_deictic_intent_passes_through_untouched():
    stream = _FakeStream([_FakeSample(0.9, 0.9)])   # would map if consulted
    intent = Intent("pause", raw_utterance="pause")

    out = FusionBinder(stream, _FakeCalib()).bind(intent)

    assert out is intent                            # untouched identity
    assert out.pointer is None
    assert stream.last_kwargs is None               # stream never consulted


def test_two_point_binds_both_pointers_from_distinct_samples():
    src, dst = _FakeSample(0.2, 0.2), _FakeSample(0.8, 0.7)
    binder = FusionBinder(_FakeStream([src, dst]), _FakeCalib())

    out = binder.bind(_deictic_move())

    assert out.pointer == (0.2 * 1440, 0.2 * 900)
    assert out.pointer2 == (0.8 * 1440, 0.7 * 900)


def test_two_point_identical_held_still_sample_stays_ambiguous():
    """A held-still hand yields the same coord twice -> no real destination."""
    held = _FakeSample(0.3, 0.3)
    # both latest() calls return the SAME coord
    binder = FusionBinder(_FakeStream([held, held]), _FakeCalib())

    out = binder.bind(_deictic_move())

    # 'move' is reversible=False, so its risk is IRREVERSIBLE regardless of the
    # pointer (checked before the ambiguous branch in Intent.risk). The fusion
    # contract we assert here is simply: unbound, and still gated on confirm.
    assert out.pointer is None and out.pointer2 is None
    assert out.must_confirm is True


def test_two_point_missing_destination_stays_unchanged():
    src = _FakeSample(0.2, 0.2)
    binder = FusionBinder(_FakeStream([src, None]), _FakeCalib())

    out = binder.bind(_deictic_move())

    assert out.pointer is None and out.pointer2 is None


def test_stream_exception_degrades_to_no_point():
    out = FusionBinder(_RaisingStream(), _FakeCalib()).bind(_deictic_click())
    assert out.pointer is None
    assert out.risk == RISK_AMBIGUOUS


def test_calibration_failure_does_not_guess():
    stream = _FakeStream([_FakeSample(0.5, 0.5)])
    out = FusionBinder(stream, _RaisingCalib()).bind(_deictic_click())
    assert out.pointer is None                      # never guess a coord on map failure
    assert out.risk == RISK_AMBIGUOUS


def test_real_pointersample_type_is_bindable():
    """Guard the duck-typed contract against the REAL dataclass shape."""
    s = PointerSample(
        x_norm=0.25, y_norm=0.75, ts=time.time(), conf=0.94, aim_ok=True,
        present=True, src_w=640, src_h=480, mirrored=True,
    )
    out = FusionBinder(_FakeStream([s]), _FakeCalib()).bind(_deictic_click())
    assert out.pointer == (0.25 * 1440, 0.75 * 900)

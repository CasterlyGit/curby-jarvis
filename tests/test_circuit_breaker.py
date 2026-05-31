"""Headless unit tests for CircuitBreaker.

All time advances are simulated via an injectable clock — no real wallclock delay.
No pyobjc, no network, no display needed.
"""
from __future__ import annotations

import importlib

import pytest

from curby_jarvis.circuit_breaker import CircuitBreaker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_clock(start: float = 0.0):
    """Return a mutable fake clock and an advance() callable."""
    state = [start]

    def clock() -> float:
        return state[0]

    def advance(seconds: float) -> None:
        state[0] += seconds

    return clock, advance


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

def test_initial_state_is_closed():
    cb = CircuitBreaker("test")
    assert cb.state == "closed"


def test_allow_when_closed():
    cb = CircuitBreaker("test")
    assert cb.allow() is True


# ---------------------------------------------------------------------------
# Transitions: closed → open
# ---------------------------------------------------------------------------

def test_stays_closed_below_fail_max():
    cb = CircuitBreaker("test", fail_max=3)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "closed"
    assert cb.allow() is True


def test_opens_at_fail_max():
    cb = CircuitBreaker("test", fail_max=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"


def test_allow_false_when_open():
    cb = CircuitBreaker("test", fail_max=1)
    cb.record_failure()
    assert cb.allow() is False


# ---------------------------------------------------------------------------
# Transitions: open → half_open → closed / re-open
# ---------------------------------------------------------------------------

def test_half_open_after_reset_timeout():
    clock, advance = make_clock()
    cb = CircuitBreaker("test", fail_max=1, reset_timeout=30.0, clock=clock)
    cb.record_failure()
    assert cb.state == "open"
    advance(31.0)
    assert cb.state == "half_open"


def test_allow_true_in_half_open():
    clock, advance = make_clock()
    cb = CircuitBreaker("test", fail_max=1, reset_timeout=10.0, clock=clock)
    cb.record_failure()
    advance(11.0)
    assert cb.allow() is True


def test_success_in_half_open_closes_breaker():
    clock, advance = make_clock()
    cb = CircuitBreaker("test", fail_max=1, reset_timeout=10.0, clock=clock)
    cb.record_failure()
    advance(11.0)
    assert cb.state == "half_open"
    cb.record_success()
    assert cb.state == "closed"
    assert cb.allow() is True


def test_failure_in_half_open_reopens():
    clock, advance = make_clock()
    cb = CircuitBreaker("test", fail_max=2, reset_timeout=10.0, clock=clock)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"
    advance(11.0)
    assert cb.state == "half_open"
    # Another failure while in half_open should re-open
    cb.record_failure()
    assert cb.state == "open"
    # And a further advance should give half_open again
    advance(11.0)
    assert cb.state == "half_open"


# ---------------------------------------------------------------------------
# record_success resets count
# ---------------------------------------------------------------------------

def test_success_resets_failure_count():
    cb = CircuitBreaker("test", fail_max=5)
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    assert cb.state == "closed"
    assert cb._failure_count == 0
    # Now failures accumulate from zero again
    cb.record_failure()
    assert cb.state == "closed"


def test_success_when_already_closed_is_noop():
    cb = CircuitBreaker("test", fail_max=5)
    cb.record_success()
    assert cb.state == "closed"


# ---------------------------------------------------------------------------
# open still open before timeout elapses
# ---------------------------------------------------------------------------

def test_still_open_just_before_timeout():
    clock, advance = make_clock()
    cb = CircuitBreaker("test", fail_max=1, reset_timeout=30.0, clock=clock)
    cb.record_failure()
    advance(29.9)
    assert cb.state == "open"
    assert cb.allow() is False


def test_half_open_exactly_at_timeout():
    clock, advance = make_clock()
    cb = CircuitBreaker("test", fail_max=1, reset_timeout=30.0, clock=clock)
    cb.record_failure()
    advance(30.0)
    assert cb.state == "half_open"


# ---------------------------------------------------------------------------
# Telemetry is best-effort — missing module must not break the breaker
# ---------------------------------------------------------------------------

def test_telemetry_failure_does_not_break_breaker(monkeypatch):
    """Even if the telemetry module raises on import, transitions still work."""
    import sys
    import types

    bad_telemetry = types.ModuleType("curby_jarvis.telemetry")
    bad_telemetry.emit = None  # not callable — will raise TypeError
    monkeypatch.setitem(sys.modules, "curby_jarvis.telemetry", bad_telemetry)

    cb = CircuitBreaker("test_telem", fail_max=1)
    cb.record_failure()
    assert cb.state == "open"
    cb.record_success()
    assert cb.state == "closed"


# ---------------------------------------------------------------------------
# Custom fail_max and reset_timeout
# ---------------------------------------------------------------------------

def test_custom_fail_max():
    cb = CircuitBreaker("x", fail_max=10)
    for _ in range(9):
        cb.record_failure()
    assert cb.state == "closed"
    cb.record_failure()
    assert cb.state == "open"


def test_custom_reset_timeout():
    clock, advance = make_clock()
    cb = CircuitBreaker("x", fail_max=1, reset_timeout=60.0, clock=clock)
    cb.record_failure()
    advance(59.0)
    assert cb.state == "open"
    advance(1.1)
    assert cb.state == "half_open"


# ---------------------------------------------------------------------------
# Headless import
# ---------------------------------------------------------------------------

def test_module_imports_headless():
    m = importlib.import_module("curby_jarvis.circuit_breaker")
    assert hasattr(m, "CircuitBreaker")

"""Circuit breaker — prevent cascading failures on flaky/network-bound connectors.

WHY: connectors that rely on a subprocess, Claude API, or osascript can wedge for
seconds when the endpoint is down. A closed breaker lets calls through; after
fail_max consecutive failures it OPENS (fast-fail for reset_timeout seconds); then
it enters HALF-OPEN and allows one trial call — success → closed, failure → open
again. All time queries go through an injectable `clock` so unit tests run without
real wallclock delay. Telemetry is best-effort + lazy-imported so a missing
telemetry module never breaks the breaker.
"""
from __future__ import annotations

import time
from typing import Callable, Optional


class CircuitBreaker:
    """Three-state circuit breaker: closed / open / half_open.

    Args:
        name: display name used in telemetry + repr.
        fail_max: consecutive failures before opening.
        reset_timeout: seconds the breaker stays open before half-open trial.
        clock: monotonic clock callable; default ``time.monotonic``. Inject a
               fake in tests to advance time without sleeping.
    """

    def __init__(
        self,
        name: str,
        fail_max: int = 5,
        reset_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        self._clock = clock

        self._failure_count: int = 0
        self._opened_at: Optional[float] = None   # set when state→open
        self._half_open: bool = False              # True during the trial window

    # -- state ----------------------------------------------------------------

    @property
    def state(self) -> str:
        """Current state as a string: 'closed' | 'open' | 'half_open'."""
        if self._opened_at is None:
            return "closed"
        elapsed = self._clock() - self._opened_at
        if elapsed >= self.reset_timeout:
            return "half_open"
        return "open"

    # -- allow ----------------------------------------------------------------

    def allow(self) -> bool:
        """Return True if the call should be attempted.

        - closed  → always True
        - open    → False (fast-fail)
        - half_open → True (one trial let through; next failure re-opens)
        """
        s = self.state
        if s == "closed":
            return True
        if s == "open":
            return False
        # half_open: allow exactly one trial
        return True

    # -- record ---------------------------------------------------------------

    def record_success(self) -> None:
        """Reset to closed state, clear failure count."""
        prev = self.state
        self._failure_count = 0
        self._opened_at = None
        self._half_open = False
        if prev != "closed":
            self._emit_transition(prev, "closed")

    def record_failure(self) -> None:
        """Increment failure count; open the breaker when fail_max is reached."""
        prev = self.state
        self._failure_count += 1
        if self._failure_count >= self.fail_max:
            if prev == "closed" or prev == "half_open":
                self._opened_at = self._clock()
                self._half_open = False
                self._emit_transition(prev, "open")
            elif prev == "open":
                # Already open — refresh the opened_at timestamp so the
                # timeout restarts from now (the half_open trial failed).
                self._opened_at = self._clock()
        # Fewer than fail_max: stay closed, accumulate count.

    # -- internals ------------------------------------------------------------

    def _emit_transition(self, prev: str, new: str) -> None:
        """Best-effort telemetry on state transition. Never raises."""
        try:
            from .telemetry import emit  # lazy — module may not exist yet
            emit(
                surface="operational",
                mechanism=self.name,
                event="breaker_transition",
                from_state=prev,
                to_state=new,
                failure_count=self._failure_count,
            )
        except Exception:
            pass

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"CircuitBreaker(name={self.name!r}, state={self.state!r}, "
            f"failures={self._failure_count}/{self.fail_max})"
        )


__all__ = ["CircuitBreaker"]

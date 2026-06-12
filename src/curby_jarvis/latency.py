"""latency.py — P95 latency budget tracker and SLO grader for curby-jarvis.

WHY: We target <1.5 s end-to-end for "fast" intents and <3.5 s P95 overall.
LatencyBudget reads the shared telemetry JSONL, computes percentile statistics
with pure stdlib (no numpy), and exposes a grade() helper so the UI can colour
latency chips green / amber / red without importing anything heavy.

Design decisions:
- Pure stdlib (statistics module for median; manual percentile by sorting).
- Injectable eventlog path and window size so tests are fully deterministic.
- Module-level P95_MS + refresh_global() for connectors that want a quick scalar.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# SLO targets (milliseconds)
TARGETS: dict[str, float] = {
    "parse_first_token_ms": 400.0,
    "route_ms": 50.0,
    "e2e_p95_ms": 3500.0,
}

# Module-level scalar, updated by refresh_global()
P95_MS: float = 0.0


def grade(total_ms: float) -> str:
    """Grade an end-to-end latency value.

    Returns:
        'green'  when total_ms <  1 500
        'amber'  when total_ms <  3 500
        'red'    when total_ms >= 3 500
    """
    if total_ms < 1500.0:
        return "green"
    if total_ms < 3500.0:
        return "amber"
    return "red"


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Return the *pct*-th percentile (0–100) from a pre-sorted list.

    Uses the nearest-rank method; returns 0.0 for an empty list.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    # nearest-rank: index = ceil(pct/100 * n) - 1 (1-based)
    import math
    idx = max(0, math.ceil(pct / 100.0 * len(sorted_values)) - 1)
    return sorted_values[idx]


class LatencyBudget:
    """Rolling P95 tracker over the last *window* events in the telemetry log.

    Args:
        eventlog: path override for the JSONL file (uses telemetry.EVENTLOG default).
        window:   how many tail events to consider (default 100).
    """

    def __init__(self, eventlog: Optional[Path] = None, window: int = 100) -> None:
        self._eventlog = eventlog
        self._window = window
        self._p95: float = 0.0
        self._count: int = 0
        self._stage_values: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def p95_ms(self) -> float:
        """Current P95 end-to-end latency in ms (from last refresh())."""
        return self._p95

    @property
    def count(self) -> int:
        """Number of events considered in the last refresh()."""
        return self._count

    def refresh(self) -> "LatencyBudget":
        """Re-read the eventlog and recompute percentile statistics.

        Safe to call at any frequency; reads at most *window* tail lines.
        Never raises.
        """
        try:
            # Lazy import so telemetry itself stays importable without deps
            from .telemetry import read_events

            events = read_events(n=self._window, eventlog=self._eventlog)
            self._count = len(events)

            # Collect end-to-end latency values
            e2e: list[float] = []
            stage_buckets: dict[str, list[float]] = {}
            for ev in events:
                lat = ev.get("latency_ms")
                if lat is not None:
                    try:
                        e2e.append(float(lat))
                    except (TypeError, ValueError):
                        pass
                # Collect stage-level timings (any key ending in _ms)
                for k, v in ev.items():
                    if k.endswith("_ms") and k != "latency_ms":
                        try:
                            stage_buckets.setdefault(k, []).append(float(v))
                        except (TypeError, ValueError):
                            pass

            e2e.sort()
            self._p95 = _percentile(e2e, 95)
            self._stage_values = {k: sorted(v) for k, v in stage_buckets.items()}
        except Exception:
            pass
        return self

    def stage_p95(self, stage: str) -> float:
        """Return the P95 for a named stage (e.g. 'route_ms', 'parse_ms').

        Returns 0.0 if no data for that stage key.
        """
        vals = self._stage_values.get(stage, [])
        return _percentile(vals, 95)

    def regressed(self, threshold: float = 0.1) -> bool:
        """True when the current P95 exceeds the SLO target by *threshold* fraction.

        Example: threshold=0.1 → True when p95_ms > e2e_p95_ms * 1.10.
        Always False when no events have been collected.
        """
        if self._count == 0:
            return False
        limit = TARGETS["e2e_p95_ms"] * (1.0 + threshold)
        return self._p95 > limit


# ------------------------------------------------------------------
# Module-level convenience
# ------------------------------------------------------------------

_global_budget: Optional[LatencyBudget] = None


def refresh_global(eventlog: Optional[Path] = None, window: int = 100) -> float:
    """Refresh (or create) the module-level LatencyBudget and update P95_MS.

    Returns the updated P95_MS value.
    """
    global _global_budget, P95_MS
    if _global_budget is None:
        _global_budget = LatencyBudget(eventlog=eventlog, window=window)
    _global_budget.refresh()
    P95_MS = _global_budget.p95_ms
    return P95_MS

"""telemetry.py — append-only JSONL event log for curby-jarvis observability.

WHY: Every connector, STT session, and LLM call emits a single JSON line here so
latency regressions, error spikes, and routing decisions surface in tooling
(HistoryWidget, LatencyBudget) without requiring a sidecar process. The file is
the SAME one router._log already writes — new keys are purely additive.

Design decisions:
- Pure stdlib: no third-party deps, safe to import in CI / headless environments.
- Never raises: telemetry must not kill the caller on a full disk / bad path.
- Dependency-injectable eventlog path so tests use temp files.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

EVENTLOG = Path("~/.curby/jarvis-events.jsonl").expanduser()

_VALID_SURFACES = {"operational", "cognitive", "contextual"}


def new_trace_id() -> str:
    """Return a fresh UUID4 hex string suitable for grouping related events."""
    return uuid.uuid4().hex


def emit(
    *,
    trace_id: Optional[str] = None,
    surface: str = "operational",
    eventlog: Optional[Path] = None,
    **fields,
) -> None:
    """Append ONE JSON line to the event log. Best-effort: never raises.

    Args:
        trace_id: optional correlation token (from new_trace_id()).
        surface:  one of 'operational' | 'cognitive' | 'contextual'.
        eventlog: override path for the JSONL file (used by tests).
        **fields: arbitrary key/value pairs merged into the log line.
    """
    try:
        path = (eventlog or EVENTLOG).expanduser() if isinstance(
            (eventlog or EVENTLOG), Path
        ) else Path(eventlog or EVENTLOG).expanduser()
        record: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trace_id": trace_id,
            "surface": surface if surface in _VALID_SURFACES else "operational",
        }
        record.update(fields)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


def read_events(n: int = 100, eventlog: Optional[Path] = None) -> list[dict]:
    """Return the last *n* parsed log lines from the JSONL file.

    Tolerates malformed / truncated lines — silently skips them.
    """
    path = (eventlog or EVENTLOG).expanduser() if isinstance(
        (eventlog or EVENTLOG), Path
    ) else Path(eventlog or EVENTLOG).expanduser()
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    results: list[dict] = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except Exception:
            continue
    return results[-n:]


def latency_breakdown(**stage_ms: float) -> dict:
    """Build a latency breakdown dict from keyword stage timings.

    Example::

        latency_breakdown(parse_ms=12.0, route_ms=3.5, execute_ms=820.0)
        # → {"parse_ms": 12.0, "route_ms": 3.5, "execute_ms": 820.0,
        #    "total_ms": 835.5}

    The caller may pass any stage names. ``total_ms`` is added automatically
    as the sum of all values.
    """
    total = sum(v for v in stage_ms.values() if isinstance(v, (int, float)))
    return {**stage_ms, "total_ms": total}

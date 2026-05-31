"""AgentFallbackConnector — the open-ended last resort (cost=10).

This is the floor of the capability chain: when no cheaper connector can serve an
utterance, hand the RAW utterance to a fresh headless `claude -p` agent run. It
serves the explicit `agent_task` verb AND acts as the capability-miss catch-all —
`can_handle` returns a small >0 confidence for ANY intent so the chain always
terminates here instead of dead-ending on an unhandled verb.

Mechanism: spawn

    claude -p --dangerously-skip-permissions <raw_utterance>

in a fresh per-task workdir ~/curby-jarvis-tasks/<ts>-<slug>/ with
start_new_session=True (detached session group so the agent owns its own process
tree and a wedged child can't reach back into the controller). We capture the
exit code: exit 0 -> ok. curby owns the full puck UI; here we just run it.

Headless contract: NOTHING at import time touches subprocess or the filesystem.
`subprocess`, the binary resolve, and the workdir mkdir all live lazily inside
execute()/helpers, so this module imports under CI with no shell and no agent
installed. execute() is wrapped in a watchdog and never raises — a failed spawn
or a timeout comes back as ConnectorResult(ok=False, ...).
"""
from __future__ import annotations

import os
import re
import time
from typing import Optional

from ..intent import RISK_AMBIGUOUS, ConnectorResult, Intent, PreviewCard
from . import Connector

# Catch-all floor: a tiny but non-zero confidence for ANY intent. The router
# orders by (cost, -confidence); at cost=10 this is always the most expensive
# connector, so it only ever wins when every cheaper candidate scored 0.0 —
# i.e. the chain would otherwise dead-end. WHY a floor at all: the controller
# must always have *somewhere* to send an utterance.
_CATCHALL_CONFIDENCE = 0.05

# Default tasks root. Each run gets a fresh <ts>-<slug>/ subdir so concurrent or
# repeated tasks never collide and the agent has a clean cwd to scribble in.
_TASKS_ROOT = os.path.expanduser("~/curby-jarvis-tasks")

# Generous watchdog: an agent task is open-ended (it may edit files, run builds).
# We still bound it so a hung `claude` can't pin the controller forever; on
# timeout we leave the detached session running and report a timeout.
_DEFAULT_TIMEOUT = 1800.0  # 30 min


def _slug(text: str, maxlen: int = 40) -> str:
    """Filesystem-safe short slug from an utterance for the workdir name."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "task"


def _resolve_claude() -> str:
    """Resolve the `claude` binary. CLAUDE_CLI overrides; else PATH lookup; else
    fall back to the bare name and let the spawn surface a clear error."""
    override = os.environ.get("CLAUDE_CLI")
    if override:
        return override
    # shutil.which is pure-Python (no subprocess) — safe to call eagerly here.
    import shutil
    found = shutil.which("claude")
    return found or "claude"


class AgentFallbackConnector(Connector):
    """Last-resort open-ended agent runner. cost=10, always available."""

    name = "agent_fallback"
    cost = 10
    use_breaker = True  # INF-15: opt into circuit breaker for failover protection

    def __init__(self, tasks_root: Optional[str] = None, timeout: float = _DEFAULT_TIMEOUT):
        # Injectable root keeps execute() testable without writing under $HOME.
        self._tasks_root = tasks_root if tasks_root is not None else _TASKS_ROOT
        self._timeout = timeout

    # -- contract ------------------------------------------------------------

    def can_handle(self, intent: Intent) -> float:
        # Never catch a sub-intent the agent loop dispatched — otherwise an
        # unroutable tool verb would spawn a fresh `claude -p` instead of being
        # reported back to the loop as an unhandled tool.
        if intent.args.get("_via_agent_loop"):
            return 0.0
        # Explicit agent task -> full confidence. Anything else -> the catch-all
        # floor so the chain always has a terminating candidate.
        if intent.verb == "agent_task":
            return 1.0
        return _CATCHALL_CONFIDENCE

    def is_available(self, intent: Intent) -> bool:
        # No TCC/permission probe to make — spawning a CLI is always available.
        # If the binary is missing, the spawn fails cleanly in execute().
        return True

    def preview(self, intent: Intent) -> PreviewCard:
        utterance = intent.raw_utterance or intent.target or intent.verb
        claude = _resolve_claude()
        # Audit literal = the exact argv we'll exec. Always confirm: an open-ended
        # agent with skipped permissions is inherently ambiguous risk (amber).
        literal = f"{claude} -p --dangerously-skip-permissions {utterance}".strip()
        return PreviewCard(
            title="agent task",
            gloss=utterance,
            mechanism=self.name,
            risk=RISK_AMBIGUOUS,
            literal=literal,
        )

    # -- argv / workdir construction (pure, testable) ------------------------

    def _build_argv(self, utterance: str) -> list[str]:
        """The exact argv to exec. Separated out so tests assert it without spawning."""
        return [_resolve_claude(), "-p", "--dangerously-skip-permissions", utterance]

    def _make_workdir(self, utterance: str) -> str:
        """Create + return a fresh ~/curby-jarvis-tasks/<ts>-<slug>/ dir."""
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        name = f"{ts}-{_slug(utterance)}"
        path = os.path.join(self._tasks_root, name)
        os.makedirs(path, exist_ok=True)
        return path

    # -- execution -----------------------------------------------------------

    def execute(self, intent: Intent) -> ConnectorResult:
        t0 = time.time()
        utterance = (intent.raw_utterance or intent.target or "").strip()
        if not utterance:
            return ConnectorResult(ok=False, mechanism=self.name, error="empty_utterance")
        try:
            workdir = self._make_workdir(utterance)
            argv = self._build_argv(utterance)
            code = self._spawn(argv, workdir)
        except Exception as e:  # a connector must never raise
            return ConnectorResult(ok=False, mechanism=self.name, error="exception", detail=repr(e))
        lat = (time.time() - t0) * 1000.0
        if code is None:
            self._record_breaker_failure()
            self._emit_telemetry(ok=False, reason="timeout", latency_ms=lat)
            return ConnectorResult(ok=False, mechanism=self.name, latency_ms=lat,
                                   error="agent_timeout", detail=str(workdir),
                                   detail_text="Agent task timed out.")
        if code == 0:
            self._record_breaker_success()
            self._emit_telemetry(ok=True, reason="exit_0", latency_ms=lat)
            return ConnectorResult(ok=True, mechanism=self.name, latency_ms=lat, detail=str(workdir),
                                   detail_text="Agent task completed successfully.")
        self._record_breaker_failure()
        self._emit_telemetry(ok=False, reason=f"exit_{code}", latency_ms=lat)
        return ConnectorResult(ok=False, mechanism=self.name, latency_ms=lat,
                               error="agent_failed", detail=f"exit={code} cwd={workdir}",
                               detail_text=f"Agent task failed with exit code {code}.")

    def _spawn(self, argv: list[str], workdir: str) -> Optional[int]:
        """Run the agent detached; return its exit code, or None on timeout.

        Lazy subprocess import keeps the module headless. start_new_session=True
        puts the agent in its own session/process group so it owns its child tree
        and signals to the controller can't propagate into it (or vice-versa)."""
        import subprocess

        proc = subprocess.Popen(
            argv,
            cwd=workdir,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
        )
        try:
            return proc.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            # Leave the detached agent running — it's mid-task and owns its own
            # session; killing it could corrupt half-applied edits. Report timeout.
            return None


    # -- INF-15: breaker + telemetry helpers ------------------------------------

    def _record_breaker_success(self) -> None:
        """Best-effort breaker record; never raises."""
        try:
            b = self.breaker
            if b is not None:
                b.record_success()
        except Exception:
            pass

    def _record_breaker_failure(self) -> None:
        """Best-effort breaker record; never raises."""
        try:
            b = self.breaker
            if b is not None:
                b.record_failure()
        except Exception:
            pass

    def _emit_telemetry(self, *, ok: bool, reason: str, latency_ms: float) -> None:
        """Best-effort operational telemetry; never raises. Lazy import so this
        module stays headless even before telemetry.py is built (module D)."""
        try:
            from ..telemetry import emit  # lazy — module may not exist yet
            emit(
                surface="operational",
                mechanism=self.name,
                ok=ok,
                reason=reason,
                latency_ms=latency_ms,
            )
        except Exception:
            pass


__all__ = ["AgentFallbackConnector"]

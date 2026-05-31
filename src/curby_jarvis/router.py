"""CapabilityRouter — ordered connector registry + per-intent fallback chain walk.

The router never touches Qt. It picks the cheapest available connector that can
handle an Intent, builds a PreviewCard, optionally asks a confirm callback, then
executes — logging (intent, mechanism, ok, latency) to a JSONL eventlog so a
mis-route or silent no-op surfaces in telemetry instead of hiding.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

from .connectors import Connector
from .intent import RISK_AMBIGUOUS, RISK_IRREVERSIBLE, ConnectorResult, Intent, PreviewCard

EVENTLOG = Path(os.path.expanduser("~/.curby/jarvis-events.jsonl"))

ConfirmFn = Callable[[PreviewCard, Intent], bool]


def _new_trace_id() -> str:
    """Per-utterance correlation id. Prefers telemetry's minter (so the format
    stays consistent) but never hard-depends on it — the module imports headless."""
    try:
        from .telemetry import new_trace_id
        return new_trace_id()
    except Exception:
        import uuid
        return uuid.uuid4().hex


def _safe(fn, *args) -> None:
    """Call a user/UI callback swallowing any error — a bad HUD hook must never
    break routing."""
    try:
        fn(*args)
    except Exception:
        pass


def _is_cancelled(token) -> bool:
    """True if a barge-in cancel token is set. Tolerates None and tokens that
    expose either .cancelled() or a truthy .is_set()."""
    if token is None:
        return False
    try:
        if hasattr(token, "cancelled"):
            return bool(token.cancelled())
        if hasattr(token, "is_set"):
            return bool(token.is_set())
    except Exception:
        return False
    return False


class CapabilityRouter:
    def __init__(self, connectors=None, eventlog: Optional[Path] = None):
        self._connectors: list[Connector] = list(connectors or [])
        self.eventlog = eventlog if eventlog is not None else EVENTLOG
        self._token_vault: dict = {}  # INF-11: tok -> (mechanism, expiry_epoch)

    def register(self, connector: Connector) -> "CapabilityRouter":
        self._connectors.append(connector)
        return self

    @property
    def connectors(self) -> list:
        """Read-only view of the registered chain (the agent loop reads this to
        build its tool palette via each connector's tool_schema())."""
        return list(self._connectors)

    # -- chain ordering -------------------------------------------------------

    def candidates(self, intent: Intent) -> list[tuple[float, Connector]]:
        """Connectors that can handle the intent, ordered cheapest+most-confident first."""
        scored = [(c.can_handle(intent), c) for c in self._connectors]
        scored = [(conf, c) for conf, c in scored if conf > 0.0]
        scored.sort(key=lambda sc: (sc[1].cost, -sc[0]))
        return scored

    def preview(self, intent: Intent) -> Optional[PreviewCard]:
        for _conf, c in self.candidates(intent):
            if c.is_available(intent):
                card = c.preview(intent)
                card.mechanism = card.mechanism or c.name
                return card
        return None

    # -- execution ------------------------------------------------------------

    def run(
        self,
        intent: Intent,
        confirm: Optional[ConfirmFn] = None,
        *,
        on_chain: Optional[Callable[[str, bool], None]] = None,
        on_event: Optional[Callable] = None,
        cancel_token=None,
        trace_id: Optional[str] = None,
    ) -> ConnectorResult:
        """Walk the chain: first available connector that can handle -> preview ->
        (confirm when required) -> execute. Falls through to the next on failure.

        All keyword args are additive and default to today's behavior:
        - on_chain(name, resolved): UI-15 radial diagnostic — fired per connector as
          the chain is walked (resolved=False before the availability probe, True if
          this connector is the one that will run).
        - on_event(ProgressEvent): INF-08/10 — when given AND the chosen connector
          supports_streaming(), execute_streaming() is used so the HUD sees live work.
        - cancel_token: INF-12 barge-in — `.cancelled()` is polled between attempts
          and just before execute; a set token aborts with error='cancelled'.
        - trace_id: INF-05 — correlates every connector attempt + cognitive LLM event
          for one utterance; minted here when not supplied.
        """
        last = ConnectorResult(ok=False, error="no_connector")
        if trace_id is None:
            trace_id = _new_trace_id()
        for _conf, c in self.candidates(intent):
            if _is_cancelled(cancel_token):
                return ConnectorResult(ok=False, mechanism="router", error="cancelled")
            if on_chain is not None:
                _safe(on_chain, c.name, False)
            if not c.is_available(intent):
                continue
            if on_chain is not None:
                _safe(on_chain, c.name, True)
            card = c.preview(intent)
            card.mechanism = card.mechanism or c.name
            gate = intent.must_confirm or card.risk in (RISK_IRREVERSIBLE, RISK_AMBIGUOUS)
            if gate and confirm is not None:
                if not confirm(card, intent):
                    res = ConnectorResult(ok=False, mechanism=c.name, error="cancelled")
                    self._log(intent, card, res, trace_id=trace_id)
                    return res
                # INF-11: mint a one-time execution grant once a human approved.
                self._mint_token(c.name)
            if _is_cancelled(cancel_token):
                return ConnectorResult(ok=False, mechanism=c.name, error="cancelled")
            t0 = time.time()
            try:
                if on_event is not None and c.supports_streaming():
                    res = c.execute_streaming(intent, on_event)
                else:
                    res = c.execute(intent)
            except Exception as e:  # a connector should never raise, but never trust it
                res = ConnectorResult(ok=False, mechanism=c.name, error="exception", detail=repr(e))
            if not res.latency_ms:
                res.latency_ms = (time.time() - t0) * 1000.0
            res.mechanism = res.mechanism or c.name
            self._log(intent, card, res, trace_id=trace_id)
            if res.ok:
                return res
            last = res
        return last

    # -- INF-11: one-time execution-grant vault (approval as a code invariant) --

    def _mint_token(self, mechanism: str) -> str:
        """Record a short-lived (30s) execution grant after a confirm passed. The
        vault makes 'a human approved THIS action' a stored fact, not just a
        conversational one — read by audit + the agent loop's per-tool gate."""
        import uuid
        tok = uuid.uuid4().hex
        self._token_vault[tok] = (mechanism, time.time() + 30.0)
        # opportunistic GC of expired grants
        now = time.time()
        self._token_vault = {k: v for k, v in self._token_vault.items() if v[1] > now}
        return tok

    # -- telemetry ------------------------------------------------------------

    def _log(self, intent: Intent, card: Optional[PreviewCard], res: ConnectorResult,
             *, trace_id: Optional[str] = None) -> None:
        try:
            self.eventlog.parent.mkdir(parents=True, exist_ok=True)
            with self.eventlog.open("a") as f:
                f.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "trace_id": trace_id,         # INF-05: correlate the whole utterance
                    "surface": "operational",
                    "verb": intent.verb,
                    "target": intent.target,
                    "needs_pointer": intent.needs_pointer,
                    "pointer": list(intent.pointer) if intent.pointer else None,
                    "confidence": round(getattr(intent, "confidence", 1.0), 3),
                    "mechanism": res.mechanism,
                    "ok": res.ok,
                    "latency_ms": round(res.latency_ms, 1),
                    "steps": getattr(res, "steps", 0),
                    "error": res.error,
                    "risk": card.risk if card else None,
                }) + "\n")
        except Exception:
            pass

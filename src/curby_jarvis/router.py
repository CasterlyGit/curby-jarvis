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


class CapabilityRouter:
    def __init__(self, connectors=None, eventlog: Optional[Path] = None):
        self._connectors: list[Connector] = list(connectors or [])
        self.eventlog = eventlog if eventlog is not None else EVENTLOG

    def register(self, connector: Connector) -> "CapabilityRouter":
        self._connectors.append(connector)
        return self

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

    def run(self, intent: Intent, confirm: Optional[ConfirmFn] = None) -> ConnectorResult:
        """Walk the chain: first available connector that can handle -> preview ->
        (confirm when required) -> execute. Falls through to the next on failure."""
        last = ConnectorResult(ok=False, error="no_connector")
        for _conf, c in self.candidates(intent):
            if not c.is_available(intent):
                continue
            card = c.preview(intent)
            card.mechanism = card.mechanism or c.name
            gate = intent.must_confirm or card.risk in (RISK_IRREVERSIBLE, RISK_AMBIGUOUS)
            if gate and confirm is not None:
                if not confirm(card, intent):
                    res = ConnectorResult(ok=False, mechanism=c.name, error="cancelled")
                    self._log(intent, card, res)
                    return res
            t0 = time.time()
            res = c.execute(intent)
            if not res.latency_ms:
                res.latency_ms = (time.time() - t0) * 1000.0
            res.mechanism = res.mechanism or c.name
            self._log(intent, card, res)
            if res.ok:
                return res
            last = res
        return last

    # -- telemetry ------------------------------------------------------------

    def _log(self, intent: Intent, card: Optional[PreviewCard], res: ConnectorResult) -> None:
        try:
            self.eventlog.parent.mkdir(parents=True, exist_ok=True)
            with self.eventlog.open("a") as f:
                f.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "verb": intent.verb,
                    "target": intent.target,
                    "needs_pointer": intent.needs_pointer,
                    "pointer": list(intent.pointer) if intent.pointer else None,
                    "mechanism": res.mechanism,
                    "ok": res.ok,
                    "latency_ms": round(res.latency_ms, 1),
                    "error": res.error,
                    "risk": card.risk if card else None,
                }) + "\n")
        except Exception:
            pass

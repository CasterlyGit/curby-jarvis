"""MCPConnectorAdapter — wraps a single MCP server tool as a Connector (cost=7).

WHY this exists: each tool advertised by an MCP server gets its own adapter so the
agent loop (cost=9) can select it by name via ``intent.args["mcp_tool"]``.  The
adapter is built by ``mcp_bridge.build_adapters()`` which injects a live ``call_tool``
callable; this keeps the adapter itself SDK-free and fully unit-testable with a fake.

``can_handle`` returns 1.0 ONLY when the intent explicitly names this tool via
``args["mcp_tool"]``. This means MCP tools are always agent-selected rather than
heuristically matched — the right tool for the job is chosen by the agent loop, not
a keyword race. Everything else scores 0.0 so MCP adapters never shadow cheaper
connectors.

Headless contract: no SDK import at module level; the mcp SDK never needs to be
installed for this file to import cleanly.
"""
from __future__ import annotations

import time
from typing import Callable

from ..intent import ConnectorResult, Intent, PreviewCard, RISK_AMBIGUOUS
from . import Connector


class MCPConnectorAdapter(Connector):
    """One MCP server tool as a Connector.  Always agent-selected (cost=7)."""

    cost = 7
    use_breaker = True

    def __init__(
        self,
        *,
        server: str,
        tool_name: str,
        input_schema: dict,
        call_tool: Callable[[str, dict], dict],
    ) -> None:
        """
        Args:
            server: display name of the MCP server (e.g. "filesystem").
            tool_name: exact name of the tool as advertised by the server.
            input_schema: JSON Schema object describing the tool's inputs.
            call_tool: synchronous callable ``(tool_name, args) -> dict``
                       injected by mcp_bridge; replaced by a fake in tests.
        """
        self._server = server
        self._tool_name = tool_name
        self._input_schema = input_schema
        self._call_tool = call_tool
        # name is a class attribute on Connector; we override per-instance.
        # Anthropic tool names must match ^[a-zA-Z0-9_-]{1,64}$ — colons are
        # rejected, so sanitize. The agent loop dispatches sub-intents with
        # verb == this sanitized name, so can_handle matches on the verb.
        import re
        self.name = re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp_{server}_{tool_name}")[:64]

    # -- Connector interface --------------------------------------------------

    def can_handle(self, intent: Intent) -> float:
        """1.0 only when the agent loop selects this tool by its sanitized name."""
        return 1.0 if intent.verb == self.name else 0.0

    def is_available(self, intent: Intent) -> bool:
        """Available while the circuit breaker is not open."""
        return self.breaker_allows()

    def preview(self, intent: Intent) -> PreviewCard:
        utterance = intent.raw_utterance or intent.target or intent.verb
        return PreviewCard(
            title=f"mcp:{self._server}",
            gloss=utterance,
            mechanism=self.name,
            risk=RISK_AMBIGUOUS,
            literal=f"{self._server}/{self._tool_name}",
        )

    def tool_schema(self) -> dict:
        """Expose this tool's description + input schema to the agent loop."""
        return {
            "name": self.name,
            "description": (
                self._input_schema.get("description")
                or f"MCP tool {self._tool_name} from server {self._server}"
            ),
            "input_schema": self._input_schema,
        }

    def execute(self, intent: Intent) -> ConnectorResult:
        """Map intent.args to call_tool, wrap result in ConnectorResult. Never raises."""
        t0 = time.monotonic()
        try:
            # Pass intent.args minus internal routing keys (mcp_tool + any
            # _-prefixed sentinels like _via_agent_loop) as the tool args.
            args = {k: v for k, v in intent.args.items()
                    if k != "mcp_tool" and not k.startswith("_")}
            result = self._call_tool(self._tool_name, args)
            lat = (time.monotonic() - t0) * 1000.0

            if isinstance(result, dict) and "error" in result:
                if self.breaker:
                    self.breaker.record_failure()
                return ConnectorResult(
                    ok=False,
                    mechanism=self.name,
                    latency_ms=lat,
                    error="mcp_tool_error",
                    detail_text=str(result["error"]),
                )

            detail = str(result) if result is not None else ""
            if self.breaker:
                self.breaker.record_success()
            return ConnectorResult(
                ok=True,
                mechanism=self.name,
                latency_ms=lat,
                detail_text=detail,
            )
        except Exception as exc:  # never raise from a connector
            lat = (time.monotonic() - t0) * 1000.0
            if self.breaker:
                try:
                    self.breaker.record_failure()
                except Exception:
                    pass
            return ConnectorResult(
                ok=False,
                mechanism=self.name,
                latency_ms=lat,
                error="exception",
                detail=repr(exc),
            )


__all__ = ["MCPConnectorAdapter"]

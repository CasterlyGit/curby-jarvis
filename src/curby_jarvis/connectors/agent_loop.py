"""AgentLoopConnector — the open Claude tool-use loop (cost=9, INF-02).

This connector runs a full Anthropic tool-use loop: it exposes every registered
connector's tool_schema() to Claude and iterates while stop_reason=='tool_use',
dispatching each tool call back through the router (injected as dispatch). This
is the thesis unlock: the LLM can plan multi-step workflows and drive any
connector by name without the user having to voice a specific verb.

Design decisions:
- dispatch + tools_provider + client_factory are constructor-injected so unit
  tests run with pure fakes — no API key, no network.
- client_factory defaults to a lazy lambda that builds anthropic.Anthropic()
  only on first call, keeping the import boundary headless.
- max_steps hard-caps the loop (default 12) so a mis-configured model can't
  pin the controller in an infinite tool chain.
- telemetry and circuit-breaker are best-effort: if their modules don't exist
  yet (built by sibling owners) we skip them silently.
- Connectors must never raise; we uphold that contract here too.

Mechanism: agent_loop (LLM orchestration)
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

from ..intent import RISK_AMBIGUOUS, ConnectorResult, Intent, PreviewCard, ProgressEvent
from . import Connector, intent_from_tool_input


class AgentLoopConnector(Connector):
    """Open Claude tool-use loop: plans + dispatches multi-step actions. cost=9."""

    name = "agent_loop"
    cost = 9
    use_breaker = True

    def __init__(
        self,
        *,
        dispatch: Optional[Callable[[Intent], ConnectorResult]] = None,
        tools_provider: Optional[Callable[[], list[dict]]] = None,
        client_factory: Optional[Callable] = None,
        confirm: Optional[Callable] = None,
        model: str = "claude-haiku-4-5-20251001",
        max_steps: int = 12,
    ):
        # dispatch is injected by P2 as router.run; None → unavailable.
        self._dispatch = dispatch
        # tools_provider returns a list of tool_schema() dicts for all connectors.
        self._tools_provider = tools_provider if tools_provider is not None else lambda: []
        # client_factory: lazily builds an Anthropic client; tests inject a fake.
        self._client_factory = client_factory
        # confirm gate forwarded to every sub-dispatch so the loop can't run a
        # RISK_IRREVERSIBLE/AMBIGUOUS tool without the human approving it.
        self._confirm = confirm
        self._model = model
        self._max_steps = max_steps
        # lazy client cache — built on first use
        self._client = None
        # Does the injected dispatch accept a 2nd (confirm) arg? router.run does;
        # a 1-arg test fake does not. Probe once so we forward confirm safely.
        self._dispatch_takes_confirm = False
        if dispatch is not None:
            try:
                import inspect
                params = inspect.signature(dispatch).parameters
                self._dispatch_takes_confirm = ("confirm" in params or len(params) >= 2)
            except (ValueError, TypeError):
                self._dispatch_takes_confirm = False

    # -- contract ---------------------------------------------------------------

    def can_handle(self, intent: Intent) -> float:
        # Never re-enter the loop on a sub-intent the loop itself dispatched
        # (prevents recursive LLM fan-out when a tool verb matches no connector).
        if intent.args.get("_via_agent_loop"):
            return 0.0
        # Full confidence for explicit agent_task; catch-all above agent_fallback's 0.05.
        if intent.verb == "agent_task":
            return 1.0
        return 0.1

    def is_available(self, intent: Intent) -> bool:
        # Requires a dispatch function (injected by P2).
        if self._dispatch is None:
            return False
        # Requires an API key, a custom client_factory (for tests), or the
        # local claude CLI binary (CURBY_BACKEND=cli or no key + claude on PATH).
        if self._client_factory is None and not os.environ.get("ANTHROPIC_API_KEY"):
            try:
                from ..claude_cli import backend_is_cli  # lazy — never at top level
                if not backend_is_cli():
                    return False
            except Exception:
                return False
        # Breaker open → unavailable.
        if not self.breaker_allows():
            return False
        return True

    def preview(self, intent: Intent) -> PreviewCard:
        utterance = intent.raw_utterance or intent.target or intent.verb
        return PreviewCard(
            title="agent",
            gloss=utterance,
            mechanism=self.name,
            risk=RISK_AMBIGUOUS,
            literal="claude agent loop",
        )

    def supports_streaming(self) -> bool:
        return True

    # -- core loop --------------------------------------------------------------

    def execute_streaming(self, intent: Intent, on_event: Callable) -> ConnectorResult:
        """Run the Anthropic tool-use loop, dispatching tool calls through the router.

        Loop contract:
        1. Build the initial user message from intent.raw_utterance.
        2. Call the LLM with tools = tools_provider().
        3. While stop_reason == 'tool_use' and steps < max_steps:
           a. For each tool_use content block, build an Intent via intent_from_tool_input,
              dispatch it, emit ProgressEvents, and add a tool_result block.
           b. Feed the updated message list back to the LLM.
        4. On end_turn or max_steps exhausted, return ok=True with detail_text + steps.
        5. On any exception, return ok=False; record breaker failure.
        """
        t0 = time.time()
        utterance = (intent.raw_utterance or intent.target or "").strip()
        if not utterance:
            return ConnectorResult(ok=False, mechanism=self.name, error="empty_utterance")

        try:
            client = self._get_client()
        except Exception as e:
            return ConnectorResult(ok=False, mechanism=self.name, error="client_init_failed",
                                   detail=repr(e))

        tools = self._tools_provider()
        messages: list[dict] = [{"role": "user", "content": utterance}]
        steps = 0
        final_text = ""

        try:
            response = self._call_llm(client, messages, tools)
        except Exception as e:
            self._record_breaker_failure()
            return ConnectorResult(ok=False, mechanism=self.name, error="llm_error",
                                   detail=repr(e))

        # Emit telemetry for the initial LLM call.
        self._emit_telemetry(response, "initial")

        while True:
            stop_reason = getattr(response, "stop_reason", None)

            # Collect final text from this response.
            for block in (response.content or []):
                if getattr(block, "type", None) == "text":
                    final_text = block.text

            if stop_reason != "tool_use":
                # end_turn or stop_sequence — loop finished.
                break

            if steps >= self._max_steps:
                # Hard cap: don't let a misconfigured model loop forever.
                break

            # Process all tool_use blocks in this response.
            tool_result_blocks = []
            assistant_content = list(response.content or [])

            for block in assistant_content:
                if getattr(block, "type", None) != "tool_use":
                    continue

                # Enforce the hard cap inside the block loop too — a single
                # response can pack >max_steps tool_use blocks (the outer while
                # check alone would let them all through).
                if steps >= self._max_steps:
                    break

                tool_id = getattr(block, "id", f"tool_{steps}")
                tool_name = getattr(block, "name", "")
                tool_input = dict(getattr(block, "input", {}) or {})

                # Emit tool_call progress event.
                try:
                    on_event(ProgressEvent(
                        phase="acting",
                        text=f"tool: {tool_name}",
                        mechanism=self.name,
                        kind="tool_call",
                    ))
                except Exception:
                    pass

                # Map the tool_use back to a routable Intent. CRITICAL: the tool
                # NAME identifies the connector, but the ACTION is the verb the
                # model chose (tool_input['verb']) — do NOT overwrite verb with the
                # tool name (that breaks can_handle for every verb-routed connector).
                # MCP adapters + computer_use are addressed by their tool name.
                model_verb = tool_input.get("verb")
                t_args = dict(tool_input.get("args") or {})
                t_args["_via_agent_loop"] = True  # block re-entry into the open-ended agents
                tname = tool_name or ""
                if tname.startswith("mcp_") or tname.startswith("mcp:"):
                    verb = tname                       # MCP adapter matches its sanitized name
                elif tname == "computer_use":
                    verb = "computer_use"
                    t_args["pixel"] = True
                else:
                    verb = model_verb or tname or "agent_task"  # regular connector: model's verb
                sub_intent = intent_from_tool_input({
                    "verb": verb,
                    "target": tool_input.get("target", ""),
                    "args": t_args,
                    "raw_utterance": intent.raw_utterance,
                })
                try:
                    if self._dispatch_takes_confirm:
                        dispatch_result = self._dispatch(sub_intent, self._confirm)
                    else:
                        dispatch_result = self._dispatch(sub_intent)
                except Exception as exc:
                    dispatch_result = ConnectorResult(ok=False, mechanism=self.name,
                                                      error="dispatch_error", detail=repr(exc))

                steps += 1

                # Emit tool_result progress event.
                try:
                    on_event(ProgressEvent(
                        phase="acting",
                        text=dispatch_result.detail_text or dispatch_result.detail or (
                            "ok" if dispatch_result.ok else dispatch_result.error),
                        mechanism=self.name,
                        kind="tool_result",
                    ))
                except Exception:
                    pass

                # Build the tool_result content block.
                if dispatch_result.ok:
                    result_content = dispatch_result.detail_text or dispatch_result.detail or "ok"
                else:
                    result_content = f"error: {dispatch_result.error}"

                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_content,
                })

            # Append assistant turn + tool results to message list.
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_result_blocks})

            # Next LLM call.
            try:
                response = self._call_llm(client, messages, tools)
                self._emit_telemetry(response, f"step_{steps}")
            except Exception as e:
                self._record_breaker_failure()
                return ConnectorResult(ok=False, mechanism=self.name, error="llm_error",
                                       detail=repr(e), steps=steps)

        lat = (time.time() - t0) * 1000.0
        self._record_breaker_success()
        return ConnectorResult(
            ok=True,
            mechanism=self.name,
            latency_ms=lat,
            detail_text=final_text,
            steps=steps,
        )

    def execute(self, intent: Intent) -> ConnectorResult:
        """Synchronous wrapper — calls execute_streaming with a no-op on_event."""
        return self.execute_streaming(intent, lambda e: None)

    # -- helpers ----------------------------------------------------------------

    def _get_client(self):
        """Return the Anthropic client (or CLI shim), building it lazily if needed."""
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
        elif not os.environ.get("ANTHROPIC_API_KEY"):
            # No API key — fall back to the local claude CLI client shim.
            # This is the zero-key path: relies on `claude auth` session.
            from ..claude_cli import ClaudeCliClient  # noqa: PLC0415
            self._client = ClaudeCliClient()
        else:
            # Lazy import: never at module level.
            import anthropic  # noqa: PLC0415
            self._client = anthropic.Anthropic()
        return self._client

    def _call_llm(self, client, messages: list[dict], tools: list[dict]):
        """Single LLM call. Lets exceptions propagate to the caller."""
        kwargs: dict = {
            "model": self._model,
            "max_tokens": 4096,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        return client.messages.create(**kwargs)

    def _emit_telemetry(self, response, label: str) -> None:
        """Best-effort telemetry emission; never raises."""
        try:
            from ..telemetry import emit  # lazy — module may not exist yet
            usage = getattr(response, "usage", None)
            emit(
                surface="cognitive",
                mechanism=self.name,
                model=self._model,
                finish_reason=getattr(response, "stop_reason", None),
                input_tokens=getattr(usage, "input_tokens", None) if usage else None,
                output_tokens=getattr(usage, "output_tokens", None) if usage else None,
                label=label,
            )
        except Exception:
            pass

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


__all__ = ["AgentLoopConnector"]

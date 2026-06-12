"""Claude CLI backend — drive the locally-authenticated `claude` binary.

This module provides a drop-in replacement for the Anthropic Python SDK client
when no ANTHROPIC_API_KEY is set. It shells out to the `claude` binary (Claude
Code) via ``claude -p --output-format json`` and parses the JSON response into
duck-typed objects that match the shapes the connectors already consume.

Design rules:
- ZERO heavy imports at module level. subprocess and json are stdlib and cheap;
  everything else is inside functions.
- Never hangs the caller: every subprocess call uses a timeout; the caller gets a
  well-defined exception (``ClaudeCliError``) on timeout or non-zero exit.
- headless-importable: no pyobjc, no PyQt, no anthropic at top level.
- CURBY_BACKEND=cli env var forces this backend unconditionally.
- When neither an API key nor the claude binary is available, callers degrade
  gracefully (connectors return None / ConnectorResult(ok=False)).

CLI invocation:
    claude -p --dangerously-skip-permissions \\
           --output-format json \\
           [--system-prompt <system>] \\
           [--model <model>] \\
           <prompt>

For intent-parse (structured output with JSON Schema):
    claude -p --dangerously-skip-permissions \\
           --output-format json \\
           --json-schema <schema_json> \\
           [--system-prompt <system>] \\
           [--model <model>] \\
           <prompt>

The ``--json-schema`` flag makes the CLI emit a ``result`` field in the JSON
response containing the validated structured object. We wrap this in a synthetic
tool-use block so ``_first_tool_input()`` in intent_parse.py can consume it
unchanged.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Any, Optional

# Default timeout for non-streaming completion calls (seconds).
# Agent tasks are open-ended; single-turn completions should be much faster.
DEFAULT_TIMEOUT = 60.0

# Env var to force the CLI backend regardless of API key presence.
_BACKEND_ENV = "CURBY_BACKEND"

# Model alias map: the CLI accepts short aliases (sonnet, haiku) and full IDs.
# We pass through whatever the caller provides — the CLI resolves aliases.
_MODEL_ALIASES = {
    "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
}


class ClaudeCliError(Exception):
    """Raised when the claude CLI call fails (timeout, non-zero exit, parse error).

    Callers in connectors should catch this and convert it to a ConnectorResult
    or None — never let it propagate to the router.
    """


# Well-known install locations, tried when PATH lookup fails. Daemons (launchd,
# conductord) often run with a bare system PATH (/usr/bin:/bin:/usr/sbin:/sbin)
# that misses the npm/homebrew bin dirs — resolution must survive that.
_FALLBACK_LOCATIONS = (
    "/usr/local/bin/claude",
    "/opt/homebrew/bin/claude",
    "~/.local/bin/claude",
)


def _resolve() -> Optional[str]:
    """CLAUDE_CLI override → PATH lookup → well-known locations → None."""
    override = os.environ.get("CLAUDE_CLI")
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return found
    for loc in _FALLBACK_LOCATIONS:
        p = os.path.expanduser(loc)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def cli_path() -> str:
    """Resolve the claude binary. CLAUDE_CLI env var overrides; else PATH lookup;
    else well-known install locations (bare-PATH daemon environments)."""
    return _resolve() or "claude"


def cli_available() -> bool:
    """True if the claude binary is resolvable to a real executable."""
    path = _resolve()
    if not path:
        return False
    return os.path.isfile(path) and os.access(path, os.X_OK)


def backend_is_cli() -> bool:
    """True when CURBY_BACKEND=cli OR (no API key AND claude binary is present)."""
    if os.environ.get(_BACKEND_ENV, "").lower() == "cli":
        return True
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return False
    return cli_available()


# ---------------------------------------------------------------------------
# Duck-typed response objects matching the anthropic SDK shape
# ---------------------------------------------------------------------------

class _Usage:
    """Mimics anthropic.types.Usage (duck-typed)."""

    def __init__(self, input_tokens: int = 0, output_tokens: int = 0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _TextBlock:
    """Mimics anthropic.types.TextBlock."""
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _ToolUseBlock:
    """Mimics anthropic.types.ToolUseBlock used in forced-tool-use responses."""
    type = "tool_use"

    def __init__(self, name: str, input_data: dict, block_id: str = "cli_tool_0"):
        self.name = name
        self.input = input_data
        self.id = block_id


class CliMessage:
    """Duck-typed equivalent of anthropic.types.Message.

    Attributes are a superset of what ``_first_tool_input()``,
    ``AgentLoopConnector._call_llm()``, and telemetry readers access.
    """

    def __init__(
        self,
        content: list[Any],
        stop_reason: str = "end_turn",
        usage: Optional[_Usage] = None,
    ):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or _Usage()


# ---------------------------------------------------------------------------
# Low-level subprocess runner
# ---------------------------------------------------------------------------

def _run_cli(
    prompt: str,
    *,
    system: Optional[str] = None,
    model: Optional[str] = None,
    json_schema: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT,
    extra_flags: Optional[list[str]] = None,
) -> dict:
    """Run ``claude -p --output-format json ...`` and return the parsed JSON dict.

    Args:
        prompt: The user prompt text.
        system: Optional system prompt string (passed via --system-prompt).
        model: Optional model identifier (passed via --model).
        json_schema: Optional JSON Schema dict for structured output (--json-schema).
        timeout: Subprocess timeout in seconds. Raises ClaudeCliError on expiry.
        extra_flags: Any additional CLI flags to append before the prompt.

    Returns:
        Parsed JSON dict from the CLI's stdout. The ``result`` field holds the
        structured output when ``json_schema`` was provided; ``content`` holds
        the conversational text otherwise.

    Raises:
        ClaudeCliError: On timeout, non-zero exit, unreadable stdout, or JSON
                        parse failure.
    """
    import subprocess  # stdlib; lazy to keep module headless-importable

    binary = cli_path()
    argv: list[str] = [
        binary, "-p",
        "--dangerously-skip-permissions",
        "--output-format", "json",
    ]

    if model:
        argv += ["--model", model]

    if system:
        argv += ["--system-prompt", system]

    if json_schema is not None:
        argv += ["--json-schema", json.dumps(json_schema, separators=(",", ":"))]

    if extra_flags:
        argv.extend(extra_flags)

    argv.append(prompt)

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError(f"claude CLI timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise ClaudeCliError(f"claude binary not found at {binary!r}") from exc
    except Exception as exc:
        raise ClaudeCliError(f"subprocess error: {exc}") from exc

    if proc.returncode != 0:
        stderr_snip = (proc.stderr or "")[:400]
        raise ClaudeCliError(
            f"claude exited {proc.returncode}: {stderr_snip}"
        )

    stdout = (proc.stdout or "").strip()
    if not stdout:
        raise ClaudeCliError("claude returned empty stdout")

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCliError(f"JSON parse failed: {exc}; raw={stdout[:200]!r}") from exc


# ---------------------------------------------------------------------------
# Intent-parse path: structured output via --json-schema
# ---------------------------------------------------------------------------

def complete_intent(
    prompt: str,
    *,
    system: Optional[str] = None,
    model: Optional[str] = None,
    intent_schema: Optional[dict] = None,
    tool_name: str = "emit_intent",
    max_tokens: int = 512,
    timeout: float = DEFAULT_TIMEOUT,
) -> CliMessage:
    """Single-turn completion for intent parsing, returning a CliMessage.

    When ``intent_schema`` is provided, the CLI is asked to produce structured
    JSON matching that schema. The result is wrapped in a synthetic _ToolUseBlock
    so ``_first_tool_input()`` in intent_parse.py sees it as a forced-tool-use
    response — no change to the parser's downstream logic.

    When ``intent_schema`` is None, the response text is returned as a _TextBlock.

    Raises ClaudeCliError on any failure; callers must catch it.
    """
    data = _run_cli(
        prompt,
        system=system,
        model=model,
        json_schema=intent_schema,
        timeout=timeout,
    )

    # The CLI's --output-format json shape:
    # {
    #   "type": "result",
    #   "result": "<text or structured object>",
    #   "session_id": "...",
    #   "cost_usd": ...,
    #   "usage": {"input_tokens": ..., "output_tokens": ...},
    #   ...
    # }
    raw_result = data.get("result", "")
    usage_data = data.get("usage") or {}
    usage = _Usage(
        input_tokens=int(usage_data.get("input_tokens", 0)),
        output_tokens=int(usage_data.get("output_tokens", 0)),
    )

    if intent_schema is not None:
        # Structured output: result is either already a dict or a JSON string.
        if isinstance(raw_result, dict):
            structured = raw_result
        else:
            try:
                structured = json.loads(str(raw_result))
            except (json.JSONDecodeError, TypeError):
                raise ClaudeCliError(
                    f"structured output parse failed; raw={raw_result!r}"
                )
        content: list[Any] = [_ToolUseBlock(name=tool_name, input_data=structured)]
    else:
        text = str(raw_result) if raw_result else ""
        content = [_TextBlock(text=text)]

    return CliMessage(content=content, stop_reason="end_turn", usage=usage)


# ---------------------------------------------------------------------------
# Agent-loop path: tool-use simulation via structured JSON plan
# ---------------------------------------------------------------------------

_AGENT_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "verb": {"type": "string"},
                    "target": {"type": "string"},
                    "args": {"type": "object"},
                    "rationale": {"type": "string"},
                },
                "required": ["tool", "verb"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["steps", "summary"],
}

_AGENT_SYSTEM = (
    "You are a macOS automation planner. Given a user command and a list of "
    "available tools, produce a JSON plan with 'steps' (ordered list of tool calls) "
    "and 'summary' (one-sentence description). Each step must specify 'tool' (the "
    "connector name), 'verb' (the action verb), optional 'target' (app/item name), "
    "optional 'args' (object), and optional 'rationale'. "
    "Return ONLY the JSON object matching the schema — no prose."
)


def plan_agent_steps(
    utterance: str,
    tools: list[dict],
    *,
    model: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Ask the CLI to plan a sequence of connector tool calls for an utterance.

    Returns a list of step dicts, each with at minimum 'tool' and 'verb' keys.
    Raises ClaudeCliError on any failure.

    This implements a pragmatic (non-iterative) alternative to the full Anthropic
    tool-use loop: the CLI returns a structured plan in one shot, then the
    AgentLoopConnector dispatches each step through the router sequentially.
    The plan is one-pass — the CLI does not see the results of earlier steps.
    For simple commands (open app, play music, close tab) this is functionally
    equivalent to the full loop. For multi-step workflows that need tool-result
    feedback, the agent_fallback (cost=10) is the better escalation path.
    """
    # Build a compact tool list for the system context.
    tool_names = [t.get("name", "") for t in tools if t.get("name")]
    tool_summary = ", ".join(tool_names) if tool_names else "(none)"
    system = _AGENT_SYSTEM + f"\n\nAvailable tools: {tool_summary}"

    data = _run_cli(
        utterance,
        system=system,
        model=model,
        json_schema=_AGENT_PLAN_SCHEMA,
        timeout=timeout,
    )

    raw_result = data.get("result", {})
    if isinstance(raw_result, str):
        try:
            raw_result = json.loads(raw_result)
        except (json.JSONDecodeError, TypeError):
            raise ClaudeCliError(f"plan parse failed; raw={raw_result!r}")

    steps = raw_result.get("steps", []) if isinstance(raw_result, dict) else []
    if not isinstance(steps, list):
        return []
    return [s for s in steps if isinstance(s, dict) and s.get("tool")]


# ---------------------------------------------------------------------------
# SDK-compatible client shim for agent_loop.py's client_factory seam
# ---------------------------------------------------------------------------

class _CliMessages:
    """Shim for ``anthropic.Anthropic().messages`` — only ``.create()`` used."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self._timeout = timeout

    def create(
        self,
        *,
        model: str = "claude-haiku-4-5-20251001",
        messages: Optional[list[dict]] = None,
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
        max_tokens: int = 4096,
        **_kwargs: Any,
    ) -> CliMessage:
        """Implement the Anthropic messages.create() interface over the CLI.

        For the agent_loop path: the CLI does a one-shot structured plan call.
        The response is a CliMessage with stop_reason='end_turn' and a _TextBlock
        summarising what was planned. The caller's tool-use loop exits immediately
        (stop_reason != 'tool_use') — tool dispatch happens via plan_agent_steps()
        which is called separately by CliAgentLoopConnector.

        For compatibility with AgentLoopConnector._call_llm(), this must return a
        CliMessage. We render the last user message as the prompt.
        """
        prompt = ""
        if messages:
            last = messages[-1]
            content = last.get("content", "")
            if isinstance(content, str):
                prompt = content
            elif isinstance(content, list):
                # list of content blocks (tool_result turns)
                parts = []
                for blk in content:
                    if isinstance(blk, dict):
                        parts.append(blk.get("content", ""))
                prompt = " ".join(str(p) for p in parts if p)

        try:
            steps = plan_agent_steps(
                prompt or "(no prompt)",
                tools or [],
                model=model,
                timeout=self._timeout,
            )
        except ClaudeCliError as exc:
            # Re-raise so AgentLoopConnector._call_llm() can catch it and
            # record the breaker failure exactly as it would for an SDK error.
            raise

        summary = f"CLI plan: {len(steps)} step(s)"
        return CliMessage(
            content=[_TextBlock(text=summary)],
            stop_reason="end_turn",
            usage=_Usage(input_tokens=0, output_tokens=0),
        )


class ClaudeCliClient:
    """SDK-compatible client shim backed by the local `claude` binary.

    Drop-in for ``anthropic.Anthropic()`` at the connector level:
    - ``client.messages.create(...)`` is the only method used by the connectors.
    - No API key needed; relies on the user's existing ``claude auth`` session.

    Inject as ``client_factory=lambda: ClaudeCliClient()`` in AgentLoopConnector.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.messages = _CliMessages(timeout=timeout)


# ---------------------------------------------------------------------------
# Intent-parse client shim (for IntentParser._ensure_client() seam)
# ---------------------------------------------------------------------------

class _CliIntentMessages:
    """Shim for IntentParser: messages.create() using --json-schema structured output."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self._timeout = timeout

    def create(
        self,
        *,
        model: str = "claude-haiku-4-5-20251001",
        messages: Optional[list[dict]] = None,
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
        tool_choice: Optional[dict] = None,
        max_tokens: int = 512,
        **_kwargs: Any,
    ) -> CliMessage:
        """Map an Anthropic messages.create() call to claude CLI --json-schema.

        IntentParser calls:
            client.messages.create(
                model=..., max_tokens=..., system=...,
                tools=[_INTENT_TOOL], tool_choice={"type": "tool", "name": "emit_intent"},
                messages=[{"role": "user", "content": user_text}],
            )

        We extract the prompt (last user message), the JSON schema (from tools[0]),
        and the tool name (from tool_choice), then call complete_intent().
        The result is a CliMessage with a _ToolUseBlock so _first_tool_input()
        in intent_parse.py works without changes.
        """
        # Extract prompt from messages list.
        prompt = ""
        if messages:
            for m in reversed(messages):
                if m.get("role") == "user":
                    content = m.get("content", "")
                    prompt = content if isinstance(content, str) else str(content)
                    break

        # Extract JSON schema from the first tool definition.
        schema: Optional[dict] = None
        tool_name = "emit_intent"
        if tools:
            schema = tools[0].get("input_schema")
        if tool_choice and isinstance(tool_choice, dict):
            tool_name = tool_choice.get("name", tool_name)

        return complete_intent(
            prompt,
            system=system,
            model=model,
            intent_schema=schema,
            tool_name=tool_name,
            max_tokens=max_tokens,
            timeout=self._timeout,
        )


class ClaudeCliIntentClient:
    """SDK-compatible client shim for IntentParser (forced-tool-use path).

    Inject as ``IntentParser(client=ClaudeCliIntentClient())`` when no API key.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.messages = _CliIntentMessages(timeout=timeout)


__all__ = [
    "ClaudeCliError",
    "ClaudeCliClient",
    "ClaudeCliIntentClient",
    "cli_available",
    "cli_path",
    "backend_is_cli",
    "complete_intent",
    "plan_agent_steps",
    "CliMessage",
    "_TextBlock",
    "_ToolUseBlock",
    "_Usage",
]

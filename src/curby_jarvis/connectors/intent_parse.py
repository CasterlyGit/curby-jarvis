"""LLM intent parser — the cold path the app hits when rule_table.lower() misses.

This is NOT a Connector: the app calls IntentParser.parse(utterance) directly and,
on a real Intent, re-routes it through the CapabilityRouter exactly like a rule-table
hit. It exists so a phrasing the regex table never anticipated ("make the window
bigger", "throw this on the big screen") still lowers into the SAME frozen Intent
shape instead of dead-ending.

Design constraints (match the Phase-0 idiom):
- Headless-importable: the anthropic SDK import is LAZY inside parse(). With no
  ANTHROPIC_API_KEY, or no SDK, or any network/SDK error, parse() returns None and
  the router falls through to AgentFallback — never raises out of parse().
- Forced tool-use: a single tool whose input_schema mirrors the Intent fields, with
  the verb constrained to the frozen VERBS set, so the model can only emit a valid,
  in-vocabulary Intent. tool_choice forces exactly that one tool call.
- Grounding: we pass the frontmost app name (ax_bridge.frontmost_pid_name) in the
  user turn so a misheard target ("clothes" -> "close") fails safe onto a real app /
  a real verb instead of hallucinating a launch of a nonexistent app.
- speculative_parse(partial): a cheaper/faster companion that fires on a partial
  transcript so the UI can speculatively preview an Intent while the user is still
  speaking. Uses the same Haiku model with a lower temperature hint and a 'best
  guess on incomplete instruction' system note. Returns None if no key, SDK absent,
  the breaker is open, or the partial text is too short to be useful.
- Internal CircuitBreaker: all LLM calls (parse + speculative_parse) go through a
  lazy internal breaker so a wedged / rate-limited endpoint trips open and the router
  can fall through to AgentFallback instead of accumulating latency.
- Telemetry: best-effort cognitive-surface emit around every LLM call: model,
  finish_reason, input_tokens, output_tokens. Import is lazy + try/except.

The model is Haiku 4.5 — cheap and ~700ms warm (prewarm.py keeps the socket hot).
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from ..intent import VERBS, Intent, has_deictic

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 512
# Speculative parse needs fewer tokens — we just want a quick verb/target guess.
_SPEC_MAX_TOKENS = 256
# Minimum non-whitespace characters in a partial before we bother calling the model.
_SPEC_MIN_CHARS = 4

# The forced tool. Its input_schema is the wire shape of an Intent: the model fills
# these fields and we build the dataclass from them. verb is an enum so the parser
# is structurally incapable of inventing a verb the connectors don't serve.
_TOOL_NAME = "emit_intent"
_INTENT_TOOL = {
    "name": _TOOL_NAME,
    "description": (
        "Emit exactly one structured Intent for the user's spoken command. "
        "Choose the single closest verb from the enum; never invent a verb. "
        "Prefer acting on the frontmost app over launching a new one."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verb": {
                "type": "string",
                "enum": sorted(VERBS),
                "description": "The closest matching action verb from the closed set.",
            },
            "target": {
                "type": "string",
                "description": (
                    "App name, search query, tab name, or thing acted on. "
                    "Empty string when the verb needs no target (e.g. copy, undo, "
                    "or a deictic 'click this')."
                ),
            },
            "args": {
                "type": "object",
                "description": (
                    "Verb-specific extras: {'dir':'up'|'down'} for volume, "
                    "{'dir':'next'|'prev'} for switch_tab, {'query':'...'} for "
                    "search/play, {'two_point':true} for move/drag. Omit if none."
                ),
            },
            "needs_pointer": {
                "type": "boolean",
                "description": (
                    "True when the command is deictic — refers to whatever the user "
                    "is pointing at (this/that/here/there/it). The pointer is bound "
                    "later from the hand-tracking stream; do NOT guess coordinates."
                ),
            },
            "reversible": {
                "type": "boolean",
                "description": (
                    "False for effects that are not trivially undoable (close, save, "
                    "move, drag, delete, send). True for everything safely reversible."
                ),
            },
            "confidence": {
                "type": "number",
                "description": (
                    "0..1 — how sure you are this verb/target match the user's intent. "
                    "Lower it when the transcript is garbled or the target is unclear."
                ),
            },
        },
        "required": ["verb", "confidence"],
    },
}

_SYSTEM = (
    "You translate a single spoken macOS command into exactly ONE Intent by calling "
    "the emit_intent tool. Rules:\n"
    "- Pick the single closest verb from the enum. Never invent a verb or a target.\n"
    "- Set needs_pointer=true for deictic commands (this/that/here/there/it) and leave "
    "target empty — the system resolves what is pointed at; do not guess.\n"
    "- Prefer acting on the FRONTMOST app the user is given over launching something "
    "new. A vague 'close it' / 'make it bigger' targets the frontmost window.\n"
    "- Set reversible=false for destructive or hard-to-undo effects (close, save, move, "
    "drag, delete, send, quit).\n"
    "- Set confidence honestly; lower it for garbled transcripts so the UI confirms.\n"
    "Always call emit_intent exactly once. Never reply with prose."
)

# System prompt variant for speculative (partial transcript) calls.
_SPEC_SYSTEM = (
    "You receive an INCOMPLETE, still-being-spoken macOS command. "
    "Best-guess the user's most likely Intent and call emit_intent once. Rules:\n"
    "- The transcript may be cut off mid-word — infer the most plausible verb/target.\n"
    "- Lower confidence significantly (≤0.6) because the input is partial.\n"
    "- Same verb/pointer/reversible rules as normal. Never invent verbs.\n"
    "- If the partial gives no useful signal (single syllable, filler word), still "
    "call emit_intent with confidence=0.0 — we'll discard it.\n"
    "Always call emit_intent exactly once. Never reply with prose."
)


class _NullBreaker:
    """Fallback no-op breaker used when circuit_breaker module isn't yet available."""

    def allow(self) -> bool:
        return True

    def record_success(self) -> None:
        pass

    def record_failure(self) -> None:
        pass


def _resolve_api_key() -> Optional[str]:
    """Env var only — the parser is opt-in on an explicit key, never silent network."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    return key or None


def _frontmost_name() -> str:
    """Frontmost app name for grounding; never raises (degrades to '' headless)."""
    try:
        from ..ax import ax_bridge
        _pid, name = ax_bridge.frontmost_pid_name()
        return name or ""
    except Exception:
        return ""


class IntentParser:
    """Forced-tool-use LLM fallback that returns the frozen Intent shape, or None.

    Inject a client for tests (`IntentParser(client=fake)`); in production the
    anthropic client is built lazily on first parse() so importing this module is
    pure-Python and headless-safe.

    An internal CircuitBreaker (lazily created) guards both parse() and
    speculative_parse() so a flaky/rate-limited endpoint trips open and the router
    falls through to AgentFallback without accumulating multi-second latency.
    """

    def __init__(self, client: Any = None, model: str = MODEL):
        self._client = client          # test seam; lazily built in production
        self._model = model
        self._breaker: Any = None      # lazy CircuitBreaker; see _ensure_breaker()

    # -- internal breaker ------------------------------------------------------

    def _ensure_breaker(self) -> Any:
        """Lazy CircuitBreaker keyed to 'intent_parse'. Import is deferred."""
        if self._breaker is None:
            try:
                from ..circuit_breaker import CircuitBreaker
                self._breaker = CircuitBreaker(name="intent_parse")
            except Exception:
                # If CircuitBreaker is not yet available (early bootstrap), no-op.
                self._breaker = _NullBreaker()
        return self._breaker

    # -- internal telemetry emit -----------------------------------------------

    def _emit(self, msg: Any, t0: float, utterance: str, tag: str = "parse") -> None:
        """Best-effort cognitive telemetry around an LLM call. Never raises."""
        try:
            from ..telemetry import emit as _emit_fn
            finish_reason = getattr(msg, "stop_reason", None)
            usage = getattr(msg, "usage", None)
            in_tok = getattr(usage, "input_tokens", None) if usage else None
            out_tok = getattr(usage, "output_tokens", None) if usage else None
            _emit_fn(
                surface="cognitive",
                mechanism="intent_parse",
                tag=tag,
                model=self._model,
                utterance_len=len(utterance),
                finish_reason=finish_reason,
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=round((time.monotonic() - t0) * 1000, 1),
            )
        except Exception:
            pass

    # -- client management -----------------------------------------------------

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        key = _resolve_api_key()
        if not key:
            return None
        try:
            import anthropic  # lazy: keeps the module headless-importable
        except Exception:
            return None
        try:
            self._client = anthropic.Anthropic(api_key=key)
        except Exception:
            self._client = None
        return self._client

    # -- parse -----------------------------------------------------------------

    def parse(self, utterance: str) -> Optional[Intent]:
        """Lower a rule-table-miss utterance into an Intent via forced tool-use.

        Returns None (so the router escalates to AgentFallback) when:
        - the utterance is empty,
        - no ANTHROPIC_API_KEY and no injected client,
        - the SDK is missing, the call fails, or the model emits no usable tool call,
        - the internal circuit breaker is open (endpoint recently failing).
        Never raises.
        """
        if not utterance or not utterance.strip():
            return None
        client = self._ensure_client()
        if client is None:
            return None

        # Fast-fail if the breaker is open.
        breaker = self._ensure_breaker()
        if not breaker.allow():
            return None

        frontmost = _frontmost_name()
        ground = (
            f"Frontmost app: {frontmost}\n" if frontmost else "Frontmost app: (unknown)\n"
        )
        user_text = ground + f'Command: "{utterance.strip()}"'

        t0 = time.monotonic()
        try:
            msg = client.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=_SYSTEM,
                tools=[_INTENT_TOOL],
                tool_choice={"type": "tool", "name": _TOOL_NAME},
                messages=[{"role": "user", "content": user_text}],
            )
        except Exception:
            # Network / auth / SDK error -> record failure, fall through to AgentFallback.
            breaker.record_failure()
            return None

        breaker.record_success()
        self._emit(msg, t0, utterance, tag="parse")

        tool_input = _first_tool_input(msg, _TOOL_NAME)
        if tool_input is None:
            return None
        return _intent_from_tool_input(tool_input, utterance)

    # -- speculative_parse -----------------------------------------------------

    def speculative_parse(self, partial: str) -> Optional[Intent]:
        """Best-guess an Intent from a partial (mid-utterance) transcript.

        Intended for the STT on_partial callback: fire speculatively so the UI
        can show an intent preview card while the user is still speaking. The
        call is intentionally cheap (lower max_tokens, short partial system prompt)
        and will be superseded by the final parse() on endpoint.

        Returns None when:
        - partial is empty or too short to be meaningful (< _SPEC_MIN_CHARS non-ws),
        - no client / no API key,
        - the circuit breaker is open,
        - any SDK / network error occurs.
        Never raises.
        """
        if not partial or len(partial.replace(" ", "")) < _SPEC_MIN_CHARS:
            return None
        client = self._ensure_client()
        if client is None:
            return None

        # Share the same breaker as parse() — one bad endpoint trips both.
        breaker = self._ensure_breaker()
        if not breaker.allow():
            return None

        frontmost = _frontmost_name()
        ground = (
            f"Frontmost app: {frontmost}\n" if frontmost else "Frontmost app: (unknown)\n"
        )
        user_text = ground + f'Partial command: "{partial.strip()}"'

        t0 = time.monotonic()
        try:
            msg = client.messages.create(
                model=self._model,
                max_tokens=_SPEC_MAX_TOKENS,
                system=_SPEC_SYSTEM,
                tools=[_INTENT_TOOL],
                tool_choice={"type": "tool", "name": _TOOL_NAME},
                messages=[{"role": "user", "content": user_text}],
            )
        except Exception:
            breaker.record_failure()
            return None

        breaker.record_success()
        self._emit(msg, t0, partial, tag="speculative_parse")

        tool_input = _first_tool_input(msg, _TOOL_NAME)
        if tool_input is None:
            return None
        return _intent_from_tool_input(tool_input, partial)


def _first_tool_input(msg: Any, tool_name: str) -> Optional[dict]:
    """Pull the input dict of the first matching tool_use block, or None."""
    content = getattr(msg, "content", None)
    if not content:
        return None
    for block in content:
        if getattr(block, "type", None) != "tool_use":
            continue
        if getattr(block, "name", None) != tool_name:
            continue
        inp = getattr(block, "input", None)
        return inp if isinstance(inp, dict) else None
    return None


def _intent_from_tool_input(data: dict, utterance: str) -> Optional[Intent]:
    """Build a frozen Intent from the tool input, defensively.

    The verb is validated against VERBS (the schema enum already constrains the
    model, but a fake/old client could send junk — fail safe to None on a bad verb).
    needs_pointer is forced on when the raw utterance is deictic even if the model
    forgot, so deixis always gates correctly downstream.
    """
    verb = str(data.get("verb", "")).strip()
    if verb not in VERBS:
        return None

    target = str(data.get("target", "") or "")
    args = data.get("args")
    args = dict(args) if isinstance(args, dict) else {}

    needs_pointer = bool(data.get("needs_pointer", False)) or has_deictic(utterance)

    # reversible defaults True; honor an explicit False from the model.
    reversible = bool(data.get("reversible", True))

    conf = data.get("confidence", 0.0)
    try:
        confidence = float(conf)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    return Intent(
        verb=verb,
        target=target,
        args=args,
        needs_pointer=needs_pointer,
        reversible=reversible,
        confidence=confidence,
        raw_utterance=utterance.strip(),
    )


__all__ = ["IntentParser", "speculative_parse"]


def speculative_parse(partial: str, *, parser: Optional["IntentParser"] = None) -> Optional[Intent]:
    """Module-level convenience: speculative_parse(partial) using a default parser.

    The default parser has no injected client (uses ANTHROPIC_API_KEY from env).
    For production use, create a single IntentParser and call its method directly.
    This wrapper exists so callers that don't manage an IntentParser instance can
    call speculative_parse(partial) without ceremony.
    """
    if parser is None:
        parser = IntentParser()
    return parser.speculative_parse(partial)

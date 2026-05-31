"""Tests for module N additions to connectors/intent_parse.py.

Covers:
- speculative_parse() via IntentParser.speculative_parse and the module-level helper
- telemetry.emit called around parse() LLM calls (cognitive surface)
- internal CircuitBreaker: breaker open → parse() returns None fast; success/failure
  recording; breaker shared between parse() and speculative_parse()

No real Anthropic client, no API key, no display, no network required.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional
import pytest

from curby_jarvis.connectors.intent_parse import IntentParser, speculative_parse
from curby_jarvis.intent import VERBS, Intent


# ---------------------------------------------------------------------------
# Fakes (mirror the existing test_intent_parse.py helpers)
# ---------------------------------------------------------------------------

class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, name, inp):
        self.name = name
        self.input = inp


class _FakeUsage:
    def __init__(self, input_tokens=10, output_tokens=20):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeMessage:
    def __init__(self, content, stop_reason="tool_use", usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or _FakeUsage()


class _FakeMessages:
    def __init__(self, content, capture, stop_reason="tool_use"):
        self._content = content
        self._capture = capture
        self._stop_reason = stop_reason
        self.call_count = 0

    def create(self, **kwargs):
        self.call_count += 1
        self._capture.update(kwargs)
        return _FakeMessage(self._content, stop_reason=self._stop_reason)


class _FakeClient:
    def __init__(self, content, stop_reason="tool_use"):
        self.captured: dict = {}
        self.messages = _FakeMessages(content, self.captured, stop_reason=stop_reason)

    @property
    def call_count(self):
        return self.messages.call_count


def _client_emitting(**intent_fields) -> _FakeClient:
    block = _ToolUseBlock("emit_intent", dict(intent_fields))
    return _FakeClient([block])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _stub_frontmost(monkeypatch):
    monkeypatch.setattr(
        "curby_jarvis.connectors.intent_parse._frontmost_name",
        lambda: "TextEdit",
    )


# ---------------------------------------------------------------------------
# speculative_parse — IntentParser method
# ---------------------------------------------------------------------------

class TestSpeculativeParse:
    def test_returns_intent_for_partial(self):
        """A partial transcript long enough to be useful returns an Intent."""
        client = _client_emitting(
            verb="fullscreen", target="", confidence=0.45, needs_pointer=False,
        )
        parser = IntentParser(client=client)
        intent = parser.speculative_parse("make it full")

        assert isinstance(intent, Intent)
        assert intent.verb == "fullscreen"
        assert intent.verb in VERBS
        # confidence must be preserved from the fake model response
        assert intent.confidence == pytest.approx(0.45)

    def test_partial_too_short_returns_none(self):
        """Single-syllable or whitespace-only partials are discarded cheaply (no LLM)."""
        client = _client_emitting(verb="open", confidence=0.9)
        parser = IntentParser(client=client)

        assert parser.speculative_parse("") is None
        assert parser.speculative_parse("   ") is None
        assert parser.speculative_parse("hi") is None  # < _SPEC_MIN_CHARS non-ws chars
        # no LLM call should have been made for the short inputs
        assert client.call_count == 0

    def test_uses_spec_system_prompt_not_full(self):
        """speculative_parse uses the _SPEC_SYSTEM prompt (contains 'INCOMPLETE')."""
        client = _client_emitting(verb="open", target="Notes", confidence=0.5)
        parser = IntentParser(client=client)
        parser.speculative_parse("open note")

        assert "INCOMPLETE" in client.captured.get("system", "")

    def test_spec_max_tokens_less_than_full_parse(self):
        """speculative_parse uses _SPEC_MAX_TOKENS (256) not MAX_TOKENS (512)."""
        from curby_jarvis.connectors.intent_parse import MAX_TOKENS, _SPEC_MAX_TOKENS

        client = _client_emitting(verb="open", confidence=0.5)
        parser = IntentParser(client=client)
        parser.speculative_parse("open not")

        assert client.captured["max_tokens"] == _SPEC_MAX_TOKENS
        assert _SPEC_MAX_TOKENS < MAX_TOKENS

    def test_no_client_no_key_returns_none(self, monkeypatch):
        """Without a client and without a key, speculative_parse returns None."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        parser = IntentParser()
        assert parser.speculative_parse("open safari") is None

    def test_sdk_error_returns_none(self):
        """SDK errors during speculative_parse never escape."""
        class _BoomMessages:
            def create(self, **kwargs):
                raise RuntimeError("network down")

        class _BoomClient:
            messages = _BoomMessages()

        parser = IntentParser(client=_BoomClient())
        assert parser.speculative_parse("open safari") is None

    def test_partial_user_text_contains_partial_label(self):
        """The user-turn content for speculative calls uses 'Partial command:' label."""
        client = _client_emitting(verb="open", confidence=0.5)
        parser = IntentParser(client=client)
        parser.speculative_parse("open saf")

        user_content = client.captured["messages"][0]["content"]
        assert "Partial command:" in user_content
        assert "open saf" in user_content

    def test_unknown_verb_returns_none(self):
        """A junk verb in the speculative response is rejected safely."""
        client = _client_emitting(verb="teleport", confidence=0.9)
        parser = IntentParser(client=client)
        assert parser.speculative_parse("teleport the window") is None


# ---------------------------------------------------------------------------
# module-level speculative_parse convenience wrapper
# ---------------------------------------------------------------------------

class TestModuleLevelSpeculativeParse:
    def test_module_level_uses_injected_parser(self):
        """module-level speculative_parse passes the parser through."""
        client = _client_emitting(verb="open", target="Notes", confidence=0.5)
        parser = IntentParser(client=client)
        intent = speculative_parse("open note", parser=parser)
        assert isinstance(intent, Intent)
        assert intent.verb == "open"

    def test_module_level_no_key_returns_none(self, monkeypatch):
        """module-level without key and without parser → None."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert speculative_parse("open safari") is None


# ---------------------------------------------------------------------------
# Telemetry emission around parse()
# ---------------------------------------------------------------------------

class TestTelemetryEmission:
    def test_emit_called_on_successful_parse(self, tmp_path, monkeypatch):
        """parse() emits a cognitive telemetry event on success."""
        eventlog = tmp_path / "events.jsonl"
        monkeypatch.setattr(
            "curby_jarvis.telemetry.EVENTLOG", eventlog
        )
        import curby_jarvis.telemetry as tel_mod
        emitted: list[dict] = []

        original_emit = tel_mod.emit

        def capturing_emit(**kwargs):
            emitted.append(kwargs)
            original_emit(eventlog=eventlog, **{k: v for k, v in kwargs.items() if k != "eventlog"})

        monkeypatch.setattr(
            "curby_jarvis.telemetry.emit",
            capturing_emit,
        )

        client = _client_emitting(verb="open", target="Notes", confidence=0.9)
        parser = IntentParser(client=client)
        intent = parser.parse("open notes")

        assert intent is not None
        # At least one cognitive emit should have been issued
        cognitive_events = [e for e in emitted if e.get("surface") == "cognitive"]
        assert len(cognitive_events) >= 1
        ev = cognitive_events[0]
        assert ev.get("mechanism") == "intent_parse"
        assert ev.get("model") is not None

    def test_emit_called_on_speculative_parse(self, tmp_path, monkeypatch):
        """speculative_parse() also emits a cognitive telemetry event on success."""
        emitted: list[dict] = []

        import curby_jarvis.telemetry as tel_mod
        original_emit = tel_mod.emit

        def capturing_emit(**kwargs):
            emitted.append(kwargs)

        monkeypatch.setattr("curby_jarvis.telemetry.emit", capturing_emit)

        client = _client_emitting(verb="open", target="Notes", confidence=0.5)
        parser = IntentParser(client=client)
        parser.speculative_parse("open not")

        cognitive = [e for e in emitted if e.get("surface") == "cognitive"]
        assert len(cognitive) >= 1
        assert cognitive[0].get("tag") == "speculative_parse"

    def test_emit_not_called_when_no_client(self, monkeypatch):
        """No LLM call → no telemetry emit (parse returned None early)."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        emitted: list[dict] = []

        import curby_jarvis.telemetry as tel_mod
        monkeypatch.setattr("curby_jarvis.telemetry.emit", lambda **kw: emitted.append(kw))

        parser = IntentParser()
        parser.parse("open notes")

        cognitive = [e for e in emitted if e.get("surface") == "cognitive"]
        assert len(cognitive) == 0

    def test_emit_includes_token_counts(self, monkeypatch):
        """Token usage from the fake message is forwarded to telemetry."""
        emitted: list[dict] = []
        import curby_jarvis.telemetry as tel_mod
        monkeypatch.setattr("curby_jarvis.telemetry.emit", lambda **kw: emitted.append(kw))

        client = _client_emitting(verb="open", confidence=0.9)
        # Override the message returned to include usage
        block = _ToolUseBlock("emit_intent", {"verb": "open", "confidence": 0.9})
        msg_with_usage = _FakeMessage([block], usage=_FakeUsage(input_tokens=55, output_tokens=30))
        client.messages._content = [block]

        class _UsageMessages:
            captured: dict = {}
            call_count = 0

            def create(self, **kwargs):
                _UsageMessages.captured.update(kwargs)
                _UsageMessages.call_count += 1
                return msg_with_usage

        client.messages = _UsageMessages()
        parser = IntentParser(client=client)
        parser.parse("open notes")

        cognitive = [e for e in emitted if e.get("surface") == "cognitive"]
        assert len(cognitive) >= 1
        ev = cognitive[0]
        assert ev.get("input_tokens") == 55
        assert ev.get("output_tokens") == 30

    def test_emit_failure_doesnt_break_parse(self, monkeypatch):
        """Even if telemetry.emit raises, parse() still returns the Intent."""
        import curby_jarvis.telemetry as tel_mod
        monkeypatch.setattr("curby_jarvis.telemetry.emit", lambda **kw: (_ for _ in ()).throw(RuntimeError("disk full")))

        client = _client_emitting(verb="open", confidence=0.9)
        parser = IntentParser(client=client)
        intent = parser.parse("open notes")
        assert intent is not None


# ---------------------------------------------------------------------------
# Internal CircuitBreaker
# ---------------------------------------------------------------------------

class TestInternalCircuitBreaker:
    def test_breaker_created_lazily(self):
        """The breaker is None before first call, then instantiated."""
        parser = IntentParser(client=_client_emitting(verb="open", confidence=0.9))
        assert parser._breaker is None
        parser.parse("open safari")
        assert parser._breaker is not None

    def test_breaker_open_blocks_parse(self):
        """When the breaker is open, parse() returns None without calling the client."""
        client = _client_emitting(verb="open", confidence=0.9)
        parser = IntentParser(client=client)

        # Force open by injecting a breaker that denies
        class _OpenBreaker:
            def allow(self):
                return False
            def record_success(self):
                pass
            def record_failure(self):
                pass

        parser._breaker = _OpenBreaker()
        result = parser.parse("open safari")

        assert result is None
        assert client.call_count == 0  # no LLM call made

    def test_breaker_open_blocks_speculative_parse(self):
        """When the breaker is open, speculative_parse() also short-circuits."""
        client = _client_emitting(verb="open", confidence=0.9)
        parser = IntentParser(client=client)

        class _OpenBreaker:
            def allow(self):
                return False
            def record_success(self):
                pass
            def record_failure(self):
                pass

        parser._breaker = _OpenBreaker()
        result = parser.speculative_parse("open safari browser")

        assert result is None
        assert client.call_count == 0

    def test_breaker_records_success_on_good_call(self):
        """A successful LLM call records success on the breaker."""
        from curby_jarvis.circuit_breaker import CircuitBreaker

        client = _client_emitting(verb="open", confidence=0.9)
        parser = IntentParser(client=client)
        # Use real CircuitBreaker
        parser._breaker = CircuitBreaker(name="test_intent_parse")

        parser.parse("open safari")
        assert parser._breaker.state == "closed"
        assert parser._breaker._failure_count == 0

    def test_breaker_records_failure_on_sdk_error(self):
        """An SDK exception records failure on the breaker."""
        from curby_jarvis.circuit_breaker import CircuitBreaker

        class _BoomMessages:
            def create(self, **kwargs):
                raise RuntimeError("timeout")

        class _BoomClient:
            messages = _BoomMessages()

        parser = IntentParser(client=_BoomClient())
        parser._breaker = CircuitBreaker(name="test_intent_parse_fail", fail_max=3)
        parser.parse("open safari")
        assert parser._breaker._failure_count == 1

    def test_breaker_trips_after_fail_max_and_blocks_calls(self):
        """After fail_max consecutive failures the breaker opens and blocks further calls."""
        from curby_jarvis.circuit_breaker import CircuitBreaker

        call_count = 0

        class _BoomMessages:
            def create(self, **kwargs):
                nonlocal call_count
                call_count += 1
                raise RuntimeError("dead endpoint")

        class _BoomClient:
            messages = _BoomMessages()

        parser = IntentParser(client=_BoomClient())
        parser._breaker = CircuitBreaker(name="test_trip", fail_max=2)

        # Two failures trip the breaker
        parser.parse("open safari")
        parser.parse("open safari")

        assert parser._breaker.state == "open"
        assert call_count == 2

        # Now the breaker is open — no further LLM call
        result = parser.parse("open safari")
        assert result is None
        assert call_count == 2  # not incremented

    def test_parse_and_speculative_share_same_breaker(self):
        """parse() and speculative_parse() share the same internal breaker instance."""
        from curby_jarvis.circuit_breaker import CircuitBreaker

        client = _client_emitting(verb="open", confidence=0.9)
        parser = IntentParser(client=client)
        parser._breaker = CircuitBreaker(name="shared_test")

        parser.parse("open safari")
        breaker_after_parse = parser._breaker

        parser.speculative_parse("open safar")
        assert parser._breaker is breaker_after_parse

    def test_speculative_parse_breaker_records_failure(self):
        """An SDK failure during speculative_parse also increments the breaker."""
        from curby_jarvis.circuit_breaker import CircuitBreaker

        class _BoomMessages:
            def create(self, **kwargs):
                raise RuntimeError("timeout")

        class _BoomClient:
            messages = _BoomMessages()

        parser = IntentParser(client=_BoomClient())
        parser._breaker = CircuitBreaker(name="spec_fail", fail_max=5)

        parser.speculative_parse("open safar")
        assert parser._breaker._failure_count == 1


# ---------------------------------------------------------------------------
# Headless import contract preserved
# ---------------------------------------------------------------------------

def test_module_still_headless_importable():
    """Adding speculative_parse + breaker + telemetry must not break headless import."""
    import importlib
    mod_name = "curby_jarvis.connectors.intent_parse"
    importlib.import_module(mod_name)
    assert "anthropic" not in sys.modules

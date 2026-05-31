"""Headless tests for the LLM intent parser.

No display, no camera, no network, no API key required: the anthropic client is
replaced by a fake that returns a canned forced-tool-use response, and the
no-key path is exercised by clearing ANTHROPIC_API_KEY. The real anthropic SDK
is never imported (it isn't installed in CI), proving the lazy-import contract.
"""
from __future__ import annotations

import pytest

from curby_jarvis.connectors.intent_parse import IntentParser
from curby_jarvis.intent import VERBS, Intent


# -- fakes mirroring the anthropic SDK response shape ------------------------

class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, name, inp):
        self.name = name
        self.input = inp


class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeMessages:
    def __init__(self, content, capture):
        self._content = content
        self._capture = capture

    def create(self, **kwargs):
        self._capture.update(kwargs)
        return _FakeMessage(self._content)


class _FakeClient:
    """Stand-in for anthropic.Anthropic — only .messages.create is used."""

    def __init__(self, content):
        self.captured: dict = {}
        self.messages = _FakeMessages(content, self.captured)


def _client_emitting(**intent_fields) -> _FakeClient:
    block = _ToolUseBlock("emit_intent", dict(intent_fields))
    return _FakeClient([block])


# -- ground the AX frontmost lookup so the test never touches pyobjc ---------

@pytest.fixture(autouse=True)
def _stub_frontmost(monkeypatch):
    monkeypatch.setattr(
        "curby_jarvis.connectors.intent_parse._frontmost_name",
        lambda: "TextEdit",
    )


# -- tests -------------------------------------------------------------------

def test_canned_tool_use_returns_intent():
    """'make the window bigger' -> a real in-vocabulary Intent built from tool input."""
    client = _client_emitting(
        verb="fullscreen", target="", args={}, needs_pointer=False,
        reversible=True, confidence=0.82,
    )
    parser = IntentParser(client=client)
    intent = parser.parse("make the window bigger")

    assert isinstance(intent, Intent)
    assert intent.verb == "fullscreen"
    assert intent.verb in VERBS
    assert intent.confidence == pytest.approx(0.82)
    assert intent.reversible is True
    assert intent.needs_pointer is False
    assert intent.raw_utterance == "make the window bigger"


def test_forced_tool_use_wiring():
    """The call forces exactly the emit_intent tool, the Haiku model, and grounds
    on the frontmost app name."""
    client = _client_emitting(verb="open", target="Safari", confidence=0.9)
    parser = IntentParser(client=client)
    parser.parse("fire up the browser")

    kw = client.captured
    assert kw["model"] == "claude-haiku-4-5-20251001"
    assert kw["tool_choice"] == {"type": "tool", "name": "emit_intent"}
    assert kw["tools"][0]["name"] == "emit_intent"
    # verb is a constrained enum drawn from the frozen VERBS set
    assert set(kw["tools"][0]["input_schema"]["properties"]["verb"]["enum"]) == set(VERBS)
    # grounding: frontmost app name is in the user turn
    assert "TextEdit" in kw["messages"][0]["content"]


def test_deictic_forces_needs_pointer_even_if_model_forgets():
    """has_deictic backstops the model: 'click this' must gate on the pointer."""
    client = _client_emitting(
        verb="click_at", target="", needs_pointer=False, confidence=0.95,
    )
    parser = IntentParser(client=client)
    intent = parser.parse("click this")

    assert intent is not None
    assert intent.needs_pointer is True
    # unresolved deixis -> ambiguous risk -> overlay forces point-and-confirm
    assert intent.risk == "ambiguous"
    assert intent.must_confirm is True


def test_irreversible_verb_from_model():
    client = _client_emitting(
        verb="close", target="window", reversible=False, confidence=0.91,
    )
    intent = IntentParser(client=client).parse("get rid of this panel")
    assert intent is not None
    assert intent.reversible is False
    assert intent.must_confirm is True


def test_no_api_key_and_no_client_returns_none(monkeypatch):
    """No injected client + no ANTHROPIC_API_KEY -> None (router uses AgentFallback)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    parser = IntentParser()  # no client injected
    assert parser.parse("make the window bigger") is None


def test_empty_utterance_returns_none():
    client = _client_emitting(verb="open", confidence=1.0)
    parser = IntentParser(client=client)
    assert parser.parse("") is None
    assert parser.parse("   ") is None


def test_unknown_verb_fails_safe_to_none():
    """A junk verb (outside VERBS) is rejected rather than producing a bad Intent."""
    client = _client_emitting(verb="teleport", confidence=0.99)
    intent = IntentParser(client=client).parse("teleport the window")
    assert intent is None


def test_no_tool_use_block_returns_none():
    """If the model replies with prose instead of a tool call, parse() returns None."""
    client = _FakeClient([_TextBlock("I cannot do that.")])
    intent = IntentParser(client=client).parse("do something")
    assert intent is None


def test_sdk_error_returns_none():
    """A raised SDK/network error never escapes parse()."""
    class _BoomMessages:
        def create(self, **kwargs):
            raise RuntimeError("network down")

    class _BoomClient:
        messages = _BoomMessages()

    intent = IntentParser(client=_BoomClient()).parse("open notes")
    assert intent is None


def test_confidence_clamped_and_coerced():
    client = _client_emitting(verb="open", target="Notes", confidence="not-a-number")
    intent = IntentParser(client=client).parse("open notes")
    assert intent is not None
    assert intent.confidence == 0.0  # bad value coerced, not crashed

    client2 = _client_emitting(verb="open", target="Notes", confidence=5.0)
    intent2 = IntentParser(client=client2).parse("open notes")
    assert intent2.confidence == 1.0  # clamped into [0,1]


def test_module_is_headless_importable():
    """Importing the module must not pull in anthropic / pyobjc at top level."""
    import sys
    import importlib

    mod_name = "curby_jarvis.connectors.intent_parse"
    importlib.import_module(mod_name)
    # the lazy contract: anthropic is not imported merely by importing the module
    assert "anthropic" not in sys.modules

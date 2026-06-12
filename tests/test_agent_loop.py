"""Headless unit tests for AgentLoopConnector.

All external dependencies are faked:
- fake_client: scripted Anthropic message responses (no network/API key).
- fake_dispatch: records calls and returns canned ConnectorResult values.
- No display, no filesystem writes, no real LLM calls.

We assert the full tool-use loop contract:
- is_available is False when dispatch is None.
- is_available is False when no API key and no client_factory.
- The loop builds the correct Intent from a tool_use block.
- dispatch is called with that Intent.
- Steps count is correct.
- ConnectorResult.ok is True on a successful end_turn after tool use.
- The final detail_text carries the assistant's last text block.
- execute() is a thin wrapper over execute_streaming.
- Never raises on any injected fault.
"""
from __future__ import annotations

import importlib
import os
import types
from typing import Callable
from unittest.mock import MagicMock

import pytest

from curby_jarvis.connectors.agent_loop import AgentLoopConnector
from curby_jarvis.intent import ConnectorResult, Intent, ProgressEvent


# ---------------------------------------------------------------------------
# Fake Anthropic client helpers
# ---------------------------------------------------------------------------

def _make_content_block(type_: str, **kwargs):
    """Build a simple namespace object acting as an Anthropic content block."""
    b = types.SimpleNamespace(type=type_, **kwargs)
    return b


def _make_response(stop_reason: str, content: list, usage=None):
    """Build a fake Anthropic messages.create() response."""
    u = types.SimpleNamespace(input_tokens=10, output_tokens=20) if usage is None else usage
    return types.SimpleNamespace(stop_reason=stop_reason, content=content, usage=u)


class FakeClient:
    """Scripted Anthropic client that returns pre-baked responses in order."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self._calls: list[dict] = []
        self.messages = self  # client.messages.create(...)

    def create(self, **kwargs) -> object:
        self._calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("FakeClient: no more scripted responses")
        return self._responses.pop(0)


def _fake_client_factory(responses: list) -> Callable:
    """Return a factory that creates a FakeClient with the given responses."""
    client = FakeClient(responses)
    return lambda: client


# ---------------------------------------------------------------------------
# Fake dispatch
# ---------------------------------------------------------------------------

class FakeDispatch:
    """Records calls and returns a canned result."""

    def __init__(self, result: ConnectorResult = None):
        self._result = result or ConnectorResult(ok=True, mechanism="fake", detail_text="done")
        self.calls: list[Intent] = []

    def __call__(self, intent: Intent) -> ConnectorResult:
        self.calls.append(intent)
        return self._result


# ---------------------------------------------------------------------------
# Helper to build a connector under test with an injected fake API key env
# ---------------------------------------------------------------------------

def make_connector(
    responses: list = None,
    dispatch=None,
    tools_provider=None,
    monkeypatch=None,
    model: str = "test-model",
    max_steps: int = 12,
) -> tuple[AgentLoopConnector, FakeClient]:
    """Build an AgentLoopConnector with fakes. Returns (connector, fake_client)."""
    if responses is None:
        # Minimal: one end_turn response with a text block.
        responses = [_make_response("end_turn", [_make_content_block("text", text="all done")])]

    client = FakeClient(responses)

    if monkeypatch:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")

    conn = AgentLoopConnector(
        dispatch=dispatch,
        tools_provider=tools_provider or (lambda: []),
        client_factory=lambda: client,
        model=model,
        max_steps=max_steps,
    )
    return conn, client


# ---------------------------------------------------------------------------
# is_available contract
# ---------------------------------------------------------------------------

def test_is_available_false_when_no_dispatch(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    conn = AgentLoopConnector(dispatch=None, client_factory=lambda: None)
    assert conn.is_available(Intent("agent_task")) is False


def test_is_available_false_when_no_key_and_no_factory_and_no_cli(monkeypatch):
    """is_available() is False when no API key, no client_factory, and no CLI backend."""
    from unittest.mock import patch
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CURBY_BACKEND", raising=False)
    dispatch = FakeDispatch()
    conn = AgentLoopConnector(dispatch=dispatch, client_factory=None)
    with patch("curby_jarvis.claude_cli.backend_is_cli", return_value=False):
        assert conn.is_available(Intent("agent_task")) is False


def test_is_available_true_with_dispatch_and_factory(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    dispatch = FakeDispatch()
    conn = AgentLoopConnector(dispatch=dispatch, client_factory=lambda: None)
    # No API key but factory present — should be available (breaker closed by default).
    assert conn.is_available(Intent("agent_task")) is True


def test_is_available_true_with_dispatch_and_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    dispatch = FakeDispatch()
    conn = AgentLoopConnector(dispatch=dispatch, client_factory=None)
    assert conn.is_available(Intent("agent_task")) is True


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------

def test_can_handle_agent_task_full():
    conn = AgentLoopConnector()
    assert conn.can_handle(Intent("agent_task")) == 1.0


def test_can_handle_other_verb_catchall():
    conn = AgentLoopConnector()
    score = conn.can_handle(Intent("open"))
    assert 0.0 < score < 1.0
    assert score == 0.1


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------

def test_preview_returns_expected_fields():
    conn = AgentLoopConnector()
    card = conn.preview(Intent("agent_task", raw_utterance="do the thing"))
    assert card.title == "agent"
    assert card.gloss == "do the thing"
    assert card.mechanism == "agent_loop"
    assert card.literal == "claude agent loop"


# ---------------------------------------------------------------------------
# Simple end_turn — no tool use
# ---------------------------------------------------------------------------

def test_execute_end_turn_no_tools(monkeypatch):
    responses = [_make_response("end_turn", [_make_content_block("text", text="Hello!")])]
    conn, client = make_connector(responses=responses, monkeypatch=monkeypatch)
    res = conn.execute(Intent("agent_task", raw_utterance="say hello"))
    assert res.ok is True
    assert res.mechanism == "agent_loop"
    assert res.detail_text == "Hello!"
    assert res.steps == 0


# ---------------------------------------------------------------------------
# Tool-use loop: one tool call then end_turn
# ---------------------------------------------------------------------------

def test_tool_use_loop_one_step(monkeypatch):
    """Verify: tool_use block → dispatch called → tool_result fed → end_turn returned."""
    tool_block = _make_content_block(
        "tool_use",
        id="tu_abc",
        name="open",
        input={"verb": "open", "target": "Spotify"},
    )
    # First response: tool_use; second: end_turn
    responses = [
        _make_response("tool_use", [tool_block]),
        _make_response("end_turn", [_make_content_block("text", text="Opened Spotify")]),
    ]

    dispatch = FakeDispatch(result=ConnectorResult(ok=True, mechanism="open", detail_text="Spotify launched"))
    conn, client = make_connector(responses=responses, dispatch=dispatch, monkeypatch=monkeypatch)

    res = conn.execute(Intent("agent_task", raw_utterance="open spotify"))
    assert res.ok is True
    assert res.steps == 1
    assert res.detail_text == "Opened Spotify"

    # dispatch was called exactly once with a valid Intent
    assert len(dispatch.calls) == 1
    called_intent = dispatch.calls[0]
    assert isinstance(called_intent, Intent)
    assert called_intent.verb == "open"
    assert called_intent.target == "Spotify"


# ---------------------------------------------------------------------------
# Progress events are emitted
# ---------------------------------------------------------------------------

def test_execute_streaming_emits_progress_events(monkeypatch):
    tool_block = _make_content_block(
        "tool_use",
        id="tu_xyz",
        name="play",
        input={"verb": "play", "target": "track"},
    )
    responses = [
        _make_response("tool_use", [tool_block]),
        _make_response("end_turn", [_make_content_block("text", text="Playing")]),
    ]

    dispatch = FakeDispatch()
    conn, _ = make_connector(responses=responses, dispatch=dispatch, monkeypatch=monkeypatch)

    events: list[ProgressEvent] = []
    res = conn.execute_streaming(Intent("agent_task", raw_utterance="play something"), events.append)

    assert res.ok is True
    kinds = [e.kind for e in events]
    assert "tool_call" in kinds
    assert "tool_result" in kinds


# ---------------------------------------------------------------------------
# max_steps prevents infinite loops
# ---------------------------------------------------------------------------

def test_max_steps_hard_cap(monkeypatch):
    """With max_steps=2 and infinite tool_use responses, stop at 2 steps."""
    def make_tool_response():
        tb = _make_content_block("tool_use", id="tu_1", name="open", input={"verb": "open", "target": "x"})
        return _make_response("tool_use", [tb])

    # 5 tool_use responses; but max_steps=2
    responses = [make_tool_response() for _ in range(5)]
    # Final end_turn after max_steps breaks the loop — but we won't call it because we break first.
    # The connector will stop after max_steps without another LLM call when the step cap fires.
    # To be safe, append a terminal response.
    responses.append(_make_response("end_turn", [_make_content_block("text", text="capped")]))

    dispatch = FakeDispatch()
    conn, _ = make_connector(responses=responses, dispatch=dispatch, max_steps=2, monkeypatch=monkeypatch)

    res = conn.execute(Intent("agent_task", raw_utterance="loop test"))
    assert res.ok is True
    # Dispatched at most max_steps times
    assert len(dispatch.calls) <= 2


# ---------------------------------------------------------------------------
# Empty utterance
# ---------------------------------------------------------------------------

def test_empty_utterance_fails_fast(monkeypatch):
    dispatch = FakeDispatch()
    conn, _ = make_connector(dispatch=dispatch, monkeypatch=monkeypatch)
    res = conn.execute(Intent("agent_task", raw_utterance=""))
    assert res.ok is False
    assert res.error == "empty_utterance"


# ---------------------------------------------------------------------------
# LLM client error → ok=False, never raises
# ---------------------------------------------------------------------------

def test_llm_error_returns_not_ok(monkeypatch):
    class BoomClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("network error")

    dispatch = FakeDispatch()
    conn = AgentLoopConnector(
        dispatch=dispatch,
        client_factory=lambda: BoomClient(),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    res = conn.execute(Intent("agent_task", raw_utterance="do something"))
    assert res.ok is False
    assert res.error == "llm_error"


# ---------------------------------------------------------------------------
# dispatch exception is caught, loop continues / terminates gracefully
# ---------------------------------------------------------------------------

def test_dispatch_error_is_caught(monkeypatch):
    def boom_dispatch(intent: Intent) -> ConnectorResult:
        raise RuntimeError("dispatch crash")

    tool_block = _make_content_block("tool_use", id="tu_err", name="open", input={"verb": "open"})
    responses = [
        _make_response("tool_use", [tool_block]),
        _make_response("end_turn", [_make_content_block("text", text="recovered")]),
    ]
    conn, _ = make_connector(responses=responses, dispatch=boom_dispatch, monkeypatch=monkeypatch)
    res = conn.execute(Intent("agent_task", raw_utterance="test dispatch error"))
    # The loop should continue and complete without raising
    assert res.ok is True


# ---------------------------------------------------------------------------
# client_init failure → ok=False, never raises
# ---------------------------------------------------------------------------

def test_client_factory_raises_returns_not_ok(monkeypatch):
    def bad_factory():
        raise RuntimeError("can't build client")

    dispatch = FakeDispatch()
    conn = AgentLoopConnector(dispatch=dispatch, client_factory=bad_factory)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    res = conn.execute(Intent("agent_task", raw_utterance="test"))
    assert res.ok is False
    assert res.error == "client_init_failed"


# ---------------------------------------------------------------------------
# tools_provider is called and its output is passed to LLM
# ---------------------------------------------------------------------------

def test_tools_provider_called(monkeypatch):
    responses = [_make_response("end_turn", [_make_content_block("text", text="ok")])]
    tool_schemas = [{"name": "open", "description": "open an app", "input_schema": {}}]
    provider_called = []

    def tools_provider():
        provider_called.append(True)
        return tool_schemas

    dispatch = FakeDispatch()
    conn, client = make_connector(responses=responses, dispatch=dispatch,
                                   tools_provider=tools_provider, monkeypatch=monkeypatch)
    conn.execute(Intent("agent_task", raw_utterance="test tools"))
    assert provider_called


# ---------------------------------------------------------------------------
# supports_streaming
# ---------------------------------------------------------------------------

def test_supports_streaming():
    assert AgentLoopConnector().supports_streaming() is True


# ---------------------------------------------------------------------------
# Headless import
# ---------------------------------------------------------------------------

def test_module_imports_headless():
    m = importlib.import_module("curby_jarvis.connectors.agent_loop")
    assert hasattr(m, "AgentLoopConnector")


# ---------------------------------------------------------------------------
# use_breaker is True
# ---------------------------------------------------------------------------

def test_use_breaker_is_true():
    assert AgentLoopConnector.use_breaker is True


def test_cost_and_name():
    conn = AgentLoopConnector()
    assert conn.cost == 9
    assert conn.name == "agent_loop"

"""Headless tests for claude_cli.py — the zero-API-key Claude CLI backend.

All subprocess calls are monkeypatched; no real `claude` binary is invoked.
Tests prove:
  1. Prompt/argv construction is correct.
  2. JSON parsing of CLI stdout works and yields the right duck-typed objects.
  3. Timeout → ClaudeCliError.
  4. Non-zero exit → ClaudeCliError.
  5. backend_is_cli() selection logic.
  6. ClaudeCliIntentClient.messages.create() returns a CliMessage with a
     _ToolUseBlock so _first_tool_input() can consume it.
  7. ClaudeCliClient.messages.create() returns a CliMessage with _TextBlock.
  8. AgentLoopConnector.is_available() is True when CLI is available + no key.
  9. AgentLoopConnector._get_client() returns a ClaudeCliClient when no key.
 10. IntentParser._ensure_client() returns a ClaudeCliIntentClient when no key.
 11. probe_agent() reports cli_backend + agent_usable correctly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cli_stdout(result_data, *, input_tokens: int = 10, output_tokens: int = 5) -> str:
    """Build a JSON string matching the `claude -p --output-format json` shape."""
    return json.dumps({
        "type": "result",
        "result": result_data,
        "session_id": "test-session",
        "cost_usd": 0.0,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })


def _completed_proc(stdout: str, returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = ""
    return proc


# ---------------------------------------------------------------------------
# Isolate ANTHROPIC_API_KEY from the real environment for every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CURBY_BACKEND", raising=False)


# ---------------------------------------------------------------------------
# 1. _run_cli — argv construction
# ---------------------------------------------------------------------------

class TestRunCli:
    def test_basic_argv_includes_required_flags(self, monkeypatch):
        """Minimal call: -p, --dangerously-skip-permissions, --output-format json."""
        from curby_jarvis.claude_cli import _run_cli

        calls: list[dict] = []

        def fake_run(argv, **kw):
            calls.append({"argv": argv, "kw": kw})
            return _completed_proc(_make_cli_stdout("hello"))

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setenv("CLAUDE_CLI", "/usr/local/bin/claude")

        result = _run_cli("say hello")

        assert len(calls) == 1
        argv = calls[0]["argv"]
        assert argv[0] == "/usr/local/bin/claude"
        assert "-p" in argv
        assert "--dangerously-skip-permissions" in argv
        assert "--output-format" in argv
        assert "json" in argv
        assert "say hello" == argv[-1]
        assert result["result"] == "hello"

    def test_system_prompt_flag(self, monkeypatch):
        from curby_jarvis.claude_cli import _run_cli

        calls: list[dict] = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return _completed_proc(_make_cli_stdout("ok"))

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setenv("CLAUDE_CLI", "claude")

        _run_cli("test", system="Be helpful.")

        argv = calls[0]
        idx = argv.index("--system-prompt")
        assert argv[idx + 1] == "Be helpful."

    def test_model_flag(self, monkeypatch):
        from curby_jarvis.claude_cli import _run_cli

        calls: list[dict] = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return _completed_proc(_make_cli_stdout("ok"))

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setenv("CLAUDE_CLI", "claude")

        _run_cli("test", model="claude-haiku-4-5-20251001")

        argv = calls[0]
        idx = argv.index("--model")
        assert argv[idx + 1] == "claude-haiku-4-5-20251001"

    def test_json_schema_flag_included_as_json_string(self, monkeypatch):
        from curby_jarvis.claude_cli import _run_cli

        calls: list[dict] = []
        schema = {"type": "object", "properties": {"verb": {"type": "string"}}}

        def fake_run(argv, **kw):
            calls.append(argv)
            return _completed_proc(_make_cli_stdout({"verb": "open"}))

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setenv("CLAUDE_CLI", "claude")

        _run_cli("open Spotify", json_schema=schema)

        argv = calls[0]
        idx = argv.index("--json-schema")
        schema_arg = json.loads(argv[idx + 1])
        assert schema_arg == schema

    def test_no_json_schema_flag_when_schema_is_none(self, monkeypatch):
        from curby_jarvis.claude_cli import _run_cli

        calls: list[dict] = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return _completed_proc(_make_cli_stdout("ok"))

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setenv("CLAUDE_CLI", "claude")

        _run_cli("test prompt")

        assert "--json-schema" not in calls[0]


# ---------------------------------------------------------------------------
# 2. JSON parsing produces correct usage
# ---------------------------------------------------------------------------

class TestJsonParsing:
    def test_usage_tokens_extracted(self, monkeypatch):
        from curby_jarvis.claude_cli import _run_cli

        monkeypatch.setenv("CLAUDE_CLI", "claude")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: _completed_proc(
                _make_cli_stdout("hi", input_tokens=42, output_tokens=7)
            ),
        )

        data = _run_cli("test")
        assert data["usage"]["input_tokens"] == 42
        assert data["usage"]["output_tokens"] == 7

    def test_complete_intent_text_mode_returns_text_block(self, monkeypatch):
        from curby_jarvis.claude_cli import _TextBlock, complete_intent

        monkeypatch.setenv("CLAUDE_CLI", "claude")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: _completed_proc(_make_cli_stdout("Hello world")),
        )

        msg = complete_intent("say something")
        assert len(msg.content) == 1
        blk = msg.content[0]
        assert isinstance(blk, _TextBlock)
        assert blk.text == "Hello world"
        assert msg.stop_reason == "end_turn"

    def test_complete_intent_structured_mode_returns_tool_use_block(self, monkeypatch):
        from curby_jarvis.claude_cli import _ToolUseBlock, complete_intent

        structured = {"verb": "open", "target": "Spotify", "confidence": 0.9}
        monkeypatch.setenv("CLAUDE_CLI", "claude")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: _completed_proc(_make_cli_stdout(structured)),
        )

        schema = {"type": "object", "properties": {"verb": {"type": "string"}}}
        msg = complete_intent("open Spotify", intent_schema=schema, tool_name="emit_intent")

        assert len(msg.content) == 1
        blk = msg.content[0]
        assert isinstance(blk, _ToolUseBlock)
        assert blk.type == "tool_use"
        assert blk.name == "emit_intent"
        assert blk.input == structured

    def test_structured_result_as_json_string_also_works(self, monkeypatch):
        from curby_jarvis.claude_cli import _ToolUseBlock, complete_intent

        structured = {"verb": "close", "confidence": 0.8}
        # CLI may return result as a JSON string rather than a nested object
        raw = json.dumps(structured)
        monkeypatch.setenv("CLAUDE_CLI", "claude")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: _completed_proc(_make_cli_stdout(raw)),
        )

        msg = complete_intent("close window", intent_schema={"type": "object"})
        blk = msg.content[0]
        assert isinstance(blk, _ToolUseBlock)
        assert blk.input["verb"] == "close"


# ---------------------------------------------------------------------------
# 3. Timeout handling
# ---------------------------------------------------------------------------

class TestTimeout:
    def test_timeout_raises_cli_error(self, monkeypatch):
        from curby_jarvis.claude_cli import ClaudeCliError, _run_cli

        def fake_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1.0)

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setenv("CLAUDE_CLI", "claude")

        with pytest.raises(ClaudeCliError, match="timed out"):
            _run_cli("prompt", timeout=1.0)

    def test_nonzero_exit_raises_cli_error(self, monkeypatch):
        from curby_jarvis.claude_cli import ClaudeCliError, _run_cli

        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: _completed_proc("", returncode=1),
        )
        monkeypatch.setenv("CLAUDE_CLI", "claude")

        with pytest.raises(ClaudeCliError, match="exited 1"):
            _run_cli("prompt")

    def test_empty_stdout_raises_cli_error(self, monkeypatch):
        from curby_jarvis.claude_cli import ClaudeCliError, _run_cli

        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: _completed_proc(""),
        )
        monkeypatch.setenv("CLAUDE_CLI", "claude")

        with pytest.raises(ClaudeCliError, match="empty stdout"):
            _run_cli("prompt")

    def test_bad_json_raises_cli_error(self, monkeypatch):
        from curby_jarvis.claude_cli import ClaudeCliError, _run_cli

        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: _completed_proc("not json {{{"),
        )
        monkeypatch.setenv("CLAUDE_CLI", "claude")

        with pytest.raises(ClaudeCliError, match="JSON parse failed"):
            _run_cli("prompt")


# ---------------------------------------------------------------------------
# 4. backend_is_cli() selection
# ---------------------------------------------------------------------------

class TestBackendSelection:
    def test_cli_forced_by_env_var(self, monkeypatch):
        from curby_jarvis.claude_cli import backend_is_cli

        monkeypatch.setenv("CURBY_BACKEND", "cli")
        # Even if no binary found, env var forces it
        assert backend_is_cli() is True

    def test_cli_not_selected_when_api_key_present(self, monkeypatch):
        from curby_jarvis.claude_cli import backend_is_cli

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        monkeypatch.delenv("CURBY_BACKEND", raising=False)
        # API key present → use real SDK, not CLI
        assert backend_is_cli() is False

    def test_cli_selected_when_no_key_and_binary_available(self, monkeypatch):
        from curby_jarvis.claude_cli import backend_is_cli

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CURBY_BACKEND", raising=False)

        # Patch cli_available to return True
        with patch("curby_jarvis.claude_cli.cli_available", return_value=True):
            result = backend_is_cli()
        assert result is True

    def test_cli_not_selected_when_no_key_and_no_binary(self, monkeypatch):
        from curby_jarvis.claude_cli import backend_is_cli

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CURBY_BACKEND", raising=False)

        with patch("curby_jarvis.claude_cli.cli_available", return_value=False):
            result = backend_is_cli()
        assert result is False


# ---------------------------------------------------------------------------
# 4b. cli_path() / cli_available() — bare-PATH daemon resolution
# ---------------------------------------------------------------------------

class TestBinaryResolution:
    """Daemons (launchd, conductord) run with PATH=/usr/bin:/bin:/usr/sbin:/sbin —
    resolution must fall back to well-known install locations, not just which()."""

    def test_env_override_wins(self, monkeypatch):
        from curby_jarvis.claude_cli import cli_path

        monkeypatch.setenv("CLAUDE_CLI", "/custom/claude")
        assert cli_path() == "/custom/claude"

    def test_falls_back_to_known_location_when_path_bare(self, monkeypatch, tmp_path):
        import curby_jarvis.claude_cli as mod

        fake = tmp_path / "claude"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.delenv("CLAUDE_CLI", raising=False)
        monkeypatch.setattr(mod.shutil, "which", lambda _: None)  # bare PATH
        monkeypatch.setattr(mod, "_FALLBACK_LOCATIONS", (str(fake),))
        assert mod.cli_path() == str(fake)
        assert mod.cli_available() is True

    def test_no_binary_anywhere_degrades(self, monkeypatch, tmp_path):
        import curby_jarvis.claude_cli as mod

        monkeypatch.delenv("CLAUDE_CLI", raising=False)
        monkeypatch.setattr(mod.shutil, "which", lambda _: None)
        monkeypatch.setattr(mod, "_FALLBACK_LOCATIONS",
                            (str(tmp_path / "nope" / "claude"),))
        assert mod.cli_path() == "claude"        # last-resort literal
        assert mod.cli_available() is False      # but availability is honest


# ---------------------------------------------------------------------------
# 5. ClaudeCliIntentClient — maps messages.create() → CliMessage + ToolUseBlock
# ---------------------------------------------------------------------------

class TestCliIntentClient:
    def _make_client(self, monkeypatch, structured_result: dict):
        """Build a ClaudeCliIntentClient with subprocess mocked."""
        from curby_jarvis.claude_cli import ClaudeCliIntentClient

        monkeypatch.setenv("CLAUDE_CLI", "claude")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: _completed_proc(_make_cli_stdout(structured_result)),
        )
        return ClaudeCliIntentClient()

    def test_returns_tool_use_block_from_structured_output(self, monkeypatch):
        from curby_jarvis.claude_cli import _ToolUseBlock

        client = self._make_client(
            monkeypatch,
            {"verb": "open", "target": "Spotify", "confidence": 0.9},
        )

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": 'Command: "open Spotify"'}],
            tools=[{
                "name": "emit_intent",
                "input_schema": {
                    "type": "object",
                    "properties": {"verb": {"type": "string"}},
                },
            }],
            tool_choice={"type": "tool", "name": "emit_intent"},
            system="You are a macOS command parser.",
            max_tokens=512,
        )

        assert msg.stop_reason == "end_turn"
        assert len(msg.content) == 1
        blk = msg.content[0]
        assert isinstance(blk, _ToolUseBlock)
        assert blk.name == "emit_intent"
        assert blk.input["verb"] == "open"
        assert blk.input["target"] == "Spotify"

    def test_first_tool_input_compat(self, monkeypatch):
        """_first_tool_input() from intent_parse.py must work on CLI's CliMessage."""
        from curby_jarvis.claude_cli import ClaudeCliIntentClient
        from curby_jarvis.connectors.intent_parse import _first_tool_input

        structured = {"verb": "close", "target": "", "confidence": 0.85}
        monkeypatch.setenv("CLAUDE_CLI", "claude")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: _completed_proc(_make_cli_stdout(structured)),
        )

        client = ClaudeCliIntentClient()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": "close window"}],
            tools=[{"name": "emit_intent", "input_schema": {"type": "object"}}],
            tool_choice={"type": "tool", "name": "emit_intent"},
        )

        inp = _first_tool_input(msg, "emit_intent")
        assert inp is not None
        assert inp["verb"] == "close"

    def test_prompt_extracted_from_last_user_message(self, monkeypatch):
        """The client must pass the last user message content as the CLI prompt."""
        from curby_jarvis.claude_cli import ClaudeCliIntentClient

        captured_argv: list[list[str]] = []

        def fake_run(argv, **kw):
            captured_argv.append(argv)
            return _completed_proc(_make_cli_stdout({"verb": "open"}))

        monkeypatch.setenv("CLAUDE_CLI", "/usr/bin/claude")
        monkeypatch.setattr("subprocess.run", fake_run)

        client = ClaudeCliIntentClient()
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            messages=[
                {"role": "user", "content": "first message"},
                {"role": "assistant", "content": "thinking..."},
                {"role": "user", "content": 'Command: "play music"'},
            ],
            tools=[{"name": "emit_intent", "input_schema": {"type": "object"}}],
        )

        argv = captured_argv[0]
        assert argv[-1] == 'Command: "play music"'


# ---------------------------------------------------------------------------
# 6. ClaudeCliClient — agent loop shim
# ---------------------------------------------------------------------------

class TestCliAgentClient:
    def test_messages_create_returns_end_turn_message(self, monkeypatch):
        from curby_jarvis.claude_cli import ClaudeCliClient

        plan = {"steps": [{"tool": "app_launch", "verb": "open", "target": "Spotify"}],
                "summary": "Open Spotify"}
        monkeypatch.setenv("CLAUDE_CLI", "claude")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: _completed_proc(_make_cli_stdout(plan)),
        )

        client = ClaudeCliClient()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": "open Spotify"}],
            tools=[{"name": "app_launch", "description": "Launch an app"}],
        )

        assert msg.stop_reason == "end_turn"
        assert len(msg.content) == 1
        assert msg.content[0].type == "text"


# ---------------------------------------------------------------------------
# 7. AgentLoopConnector.is_available() with CLI
# ---------------------------------------------------------------------------

class TestAgentLoopIsAvailable:
    def _make_conn(self):
        from curby_jarvis.connectors.agent_loop import AgentLoopConnector
        from curby_jarvis.intent import Intent

        dispatch = lambda i: None  # noqa: E731
        tools = lambda: []  # noqa: E731
        return AgentLoopConnector(dispatch=dispatch, tools_provider=tools), Intent("agent_task")

    def test_available_when_api_key_present(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-key")
        conn, intent = self._make_conn()
        assert conn.is_available(intent) is True

    def test_available_when_cli_backend_active(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch("curby_jarvis.claude_cli.backend_is_cli", return_value=True):
            conn, intent = self._make_conn()
            assert conn.is_available(intent) is True

    def test_not_available_when_no_key_and_no_cli(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch("curby_jarvis.claude_cli.backend_is_cli", return_value=False):
            conn, intent = self._make_conn()
            assert conn.is_available(intent) is False


# ---------------------------------------------------------------------------
# 8. AgentLoopConnector._get_client() returns ClaudeCliClient when no key
# ---------------------------------------------------------------------------

class TestAgentLoopGetClient:
    def test_get_client_returns_cli_client_when_no_key(self, monkeypatch):
        from curby_jarvis.claude_cli import ClaudeCliClient
        from curby_jarvis.connectors.agent_loop import AgentLoopConnector

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        conn = AgentLoopConnector(
            dispatch=lambda i: None,
            tools_provider=lambda: [],
        )
        # Force internal client to None (fresh)
        conn._client = None

        client = conn._get_client()
        assert isinstance(client, ClaudeCliClient)

    def test_get_client_prefers_injected_factory(self, monkeypatch):
        from curby_jarvis.connectors.agent_loop import AgentLoopConnector

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        sentinel = object()
        conn = AgentLoopConnector(
            dispatch=lambda i: None,
            tools_provider=lambda: [],
            client_factory=lambda: sentinel,
        )
        conn._client = None

        client = conn._get_client()
        assert client is sentinel


# ---------------------------------------------------------------------------
# 9. IntentParser._ensure_client() falls back to ClaudeCliIntentClient
# ---------------------------------------------------------------------------

class TestIntentParserCliClient:
    def test_ensure_client_returns_cli_client_when_no_key(self, monkeypatch):
        from curby_jarvis.claude_cli import ClaudeCliIntentClient
        from curby_jarvis.connectors.intent_parse import IntentParser

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with patch("curby_jarvis.claude_cli.backend_is_cli", return_value=True):
            parser = IntentParser()
            parser._client = None
            client = parser._ensure_client()

        assert isinstance(client, ClaudeCliIntentClient)

    def test_ensure_client_returns_none_when_no_key_no_cli(self, monkeypatch):
        from curby_jarvis.connectors.intent_parse import IntentParser

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with patch("curby_jarvis.claude_cli.backend_is_cli", return_value=False):
            parser = IntentParser()
            parser._client = None
            client = parser._ensure_client()

        assert client is None

    def test_ensure_client_uses_real_sdk_when_key_present(self, monkeypatch):
        """When API key is set, _ensure_client() uses the Anthropic SDK, not CLI."""
        from curby_jarvis.connectors.intent_parse import IntentParser

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")

        # Stub the Anthropic class to avoid real imports
        fake_anthropic_mod = types.ModuleType("anthropic")
        fake_client = object()
        fake_anthropic_mod.Anthropic = lambda api_key=None: fake_client

        with patch.dict(sys.modules, {"anthropic": fake_anthropic_mod}):
            parser = IntentParser()
            parser._client = None
            client = parser._ensure_client()

        assert client is fake_client


# ---------------------------------------------------------------------------
# 10. probe_agent() reports cli_backend and agent_usable
# ---------------------------------------------------------------------------

class TestProbeAgent:
    def test_cli_backend_true_when_cli_available_no_key(self, monkeypatch):
        from curby_jarvis.permissions import probe_agent

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CLI", raising=False)

        with patch("curby_jarvis.claude_cli.backend_is_cli", return_value=True):
            result = probe_agent()

        assert result["cli_backend"] is True
        assert result["agent_usable"] is True
        assert result["api_key"] is False

    def test_agent_usable_true_when_api_key_present(self, monkeypatch):
        from curby_jarvis.permissions import probe_agent

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-key")

        result = probe_agent()

        assert result["api_key"] is True
        assert result["agent_usable"] is True

    def test_agent_usable_false_when_no_key_no_cli(self, monkeypatch):
        from curby_jarvis.permissions import probe_agent

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with patch("curby_jarvis.claude_cli.backend_is_cli", return_value=False):
            result = probe_agent()

        assert result["cli_backend"] is False
        assert result["agent_usable"] is False

    def test_backward_compat_keys_present(self, monkeypatch):
        """claude_cli and api_key keys must still be present for compat."""
        from curby_jarvis.permissions import probe_agent

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        with patch("curby_jarvis.claude_cli.backend_is_cli", return_value=False):
            result = probe_agent()

        assert "claude_cli" in result
        assert "api_key" in result
        assert "cli_backend" in result
        assert "agent_usable" in result


# ---------------------------------------------------------------------------
# 11. plan_agent_steps() integration
# ---------------------------------------------------------------------------

class TestPlanAgentSteps:
    def test_steps_extracted_from_plan(self, monkeypatch):
        from curby_jarvis.claude_cli import plan_agent_steps

        plan = {
            "steps": [
                {"tool": "app_launch", "verb": "open", "target": "Spotify"},
                {"tool": "media_transport", "verb": "play", "target": ""},
            ],
            "summary": "Open Spotify and play music",
        }
        monkeypatch.setenv("CLAUDE_CLI", "claude")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: _completed_proc(_make_cli_stdout(plan)),
        )

        tools = [{"name": "app_launch"}, {"name": "media_transport"}]
        steps = plan_agent_steps("open Spotify and play music", tools)

        assert len(steps) == 2
        assert steps[0]["tool"] == "app_launch"
        assert steps[1]["verb"] == "play"

    def test_empty_steps_on_bad_plan(self, monkeypatch):
        from curby_jarvis.claude_cli import plan_agent_steps

        monkeypatch.setenv("CLAUDE_CLI", "claude")
        # Malformed plan — result is a plain string
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: _completed_proc(
                _make_cli_stdout({"steps": "not a list", "summary": "bad"})
            ),
        )

        steps = plan_agent_steps("do something", [])
        assert steps == []

    def test_cli_error_propagates(self, monkeypatch):
        from curby_jarvis.claude_cli import ClaudeCliError, plan_agent_steps

        monkeypatch.setenv("CLAUDE_CLI", "claude")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **kw: _completed_proc("", returncode=1),
        )

        with pytest.raises(ClaudeCliError):
            plan_agent_steps("do something", [])

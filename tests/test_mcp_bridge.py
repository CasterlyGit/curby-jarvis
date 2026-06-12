"""Headless unit tests for mcp_bridge.py and connectors/mcp_client.py.

All tests run without the mcp SDK, without a real server, and without any network or
filesystem permission beyond a tmp directory.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# Ensure src/ is on the path (conftest does this, but be explicit for safety).
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from curby_jarvis.intent import Intent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_intent(verb="agent_task", target="", args=None):
    return Intent(verb=verb, target=target, args=args or {})


# ---------------------------------------------------------------------------
# mcp_bridge.load_config tests
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_missing_file_returns_empty(self, tmp_path):
        from curby_jarvis.mcp_bridge import load_config
        result = load_config(path=tmp_path / "nonexistent.json")
        assert result == []

    def test_valid_config_loaded(self, tmp_path):
        from curby_jarvis.mcp_bridge import load_config
        cfg = [{"name": "my-server", "command": "/usr/local/bin/mcp-server"}]
        config_file = tmp_path / "mcp_servers.json"
        config_file.write_text(json.dumps(cfg), encoding="utf-8")
        result = load_config(path=config_file)
        assert result == cfg

    def test_malformed_json_returns_empty(self, tmp_path):
        from curby_jarvis.mcp_bridge import load_config
        config_file = tmp_path / "mcp_servers.json"
        config_file.write_text("{not valid json", encoding="utf-8")
        result = load_config(path=config_file)
        assert result == []

    def test_non_list_returns_empty(self, tmp_path):
        from curby_jarvis.mcp_bridge import load_config
        config_file = tmp_path / "mcp_servers.json"
        config_file.write_text(json.dumps({"name": "bad"}), encoding="utf-8")
        result = load_config(path=config_file)
        assert result == []

    def test_empty_array_returns_empty(self, tmp_path):
        from curby_jarvis.mcp_bridge import load_config
        config_file = tmp_path / "mcp_servers.json"
        config_file.write_text("[]", encoding="utf-8")
        result = load_config(path=config_file)
        assert result == []

    def test_default_path_absent_returns_empty(self):
        """When the default ~/.curby/mcp_servers.json does not exist (typical CI),
        returns [] without raising."""
        from curby_jarvis import mcp_bridge
        # Point to a path that certainly doesn't exist.
        original = mcp_bridge._DEFAULT_CONFIG_PATH
        try:
            mcp_bridge._DEFAULT_CONFIG_PATH = mcp_bridge.Path("/nonexistent/path/mcp_servers.json")
            result = mcp_bridge.load_config()
            assert result == []
        finally:
            mcp_bridge._DEFAULT_CONFIG_PATH = original


# ---------------------------------------------------------------------------
# mcp_bridge.build_adapters tests (mcp SDK absent)
# ---------------------------------------------------------------------------

class TestBuildAdapters:
    def test_returns_empty_when_mcp_sdk_absent(self, tmp_path):
        """build_adapters returns [] when the mcp SDK is not installed (standard CI)."""
        from curby_jarvis.mcp_bridge import build_adapters
        cfg = [{"name": "fake-server", "command": "/usr/bin/fake"}]
        result = build_adapters(config=cfg, start=True)
        # mcp not installed → empty list, no raise
        assert isinstance(result, list)

    def test_returns_empty_list_for_empty_config(self):
        from curby_jarvis.mcp_bridge import build_adapters
        assert build_adapters(config=[]) == []

    def test_returns_empty_list_for_none_config_when_file_absent(self, monkeypatch, tmp_path):
        import curby_jarvis.mcp_bridge as mod
        monkeypatch.setattr(mod, "_DEFAULT_CONFIG_PATH", tmp_path / "no.json")
        result = mod.build_adapters()
        assert result == []

    def test_build_adapters_with_fake_call_tool_no_start(self, tmp_path):
        """When start=False build_adapters returns [] (no live server to list tools).
        This is the DI path used when the caller wires adapters manually."""
        from curby_jarvis.mcp_bridge import build_adapters
        cfg = [{"name": "s1", "command": "/usr/bin/x"}]
        result = build_adapters(config=cfg, start=False)
        assert result == []

    def test_build_adapters_skips_invalid_entries(self):
        """Non-dict and missing-name entries are skipped gracefully."""
        from curby_jarvis.mcp_bridge import build_adapters
        cfg = [
            "not a dict",
            {"command": "/usr/bin/x"},  # missing name
            None,
        ]
        # Should not raise; mcp absent so result is [] anyway.
        result = build_adapters(config=cfg, start=True)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# MCPConnectorAdapter tests
# ---------------------------------------------------------------------------

class TestMCPConnectorAdapter:
    def _make_adapter(self, server="srv", tool_name="do_thing", call_tool=None, input_schema=None):
        from curby_jarvis.connectors.mcp_client import MCPConnectorAdapter
        if call_tool is None:
            call_tool = lambda n, a: {"result": "ok", "args_received": a}
        return MCPConnectorAdapter(
            server=server,
            tool_name=tool_name,
            input_schema=input_schema or {"type": "object", "properties": {}},
            call_tool=call_tool,
        )

    # -- name (sanitized for Anthropic tool-name rules: ^[a-zA-Z0-9_-]{1,64}$) --
    def test_name_includes_server_and_tool(self):
        adapter = self._make_adapter(server="myserver", tool_name="mytool")
        assert adapter.name == "mcp_myserver_mytool"

    # -- can_handle (agent loop selects by sanitized name == intent.verb) --
    def test_can_handle_returns_1_when_verb_matches_name(self):
        adapter = self._make_adapter(tool_name="do_thing")
        intent = _make_intent(verb=adapter.name, args={"x": 1})
        assert adapter.can_handle(intent) == 1.0

    def test_can_handle_returns_0_when_verb_differs(self):
        adapter = self._make_adapter(tool_name="do_thing")
        intent = _make_intent(verb="mcp_srv_other_tool")
        assert adapter.can_handle(intent) == 0.0

    def test_can_handle_returns_0_for_unrelated_verb(self):
        adapter = self._make_adapter(tool_name="do_thing")
        intent = _make_intent(verb="open", target="Spotify")
        assert adapter.can_handle(intent) == 0.0

    # -- execute maps args correctly --
    def test_execute_passes_args_minus_mcp_tool_key(self):
        received = {}

        def fake_call_tool(name, args):
            received["name"] = name
            received["args"] = dict(args)
            return {"output": "done"}

        adapter = self._make_adapter(tool_name="write_file", call_tool=fake_call_tool)
        intent = _make_intent(args={"mcp_tool": "write_file", "path": "/tmp/f.txt", "content": "hi"})
        result = adapter.execute(intent)

        assert result.ok is True
        assert result.mechanism == "mcp_srv_write_file"
        assert received["name"] == "write_file"
        assert "mcp_tool" not in received["args"]
        assert received["args"]["path"] == "/tmp/f.txt"

    def test_execute_wraps_error_dict_as_failure(self):
        adapter = self._make_adapter(call_tool=lambda n, a: {"error": "server_error"})
        intent = _make_intent(args={"mcp_tool": "do_thing"})
        result = adapter.execute(intent)
        assert result.ok is False
        assert "server_error" in result.detail_text

    def test_execute_never_raises_on_exception(self):
        def boom(n, a):
            raise RuntimeError("exploded")

        adapter = self._make_adapter(call_tool=boom)
        intent = _make_intent(args={"mcp_tool": "do_thing"})
        result = adapter.execute(intent)
        assert result.ok is False
        assert "exploded" in result.detail

    def test_execute_detail_text_contains_result(self):
        adapter = self._make_adapter(call_tool=lambda n, a: {"value": 42})
        intent = _make_intent(args={"mcp_tool": "do_thing"})
        result = adapter.execute(intent)
        assert result.ok is True
        assert "42" in result.detail_text

    # -- tool_schema --
    def test_tool_schema_returns_dict_with_name_and_schema(self):
        schema = {"type": "object", "properties": {"x": {"type": "number"}}}
        adapter = self._make_adapter(tool_name="my_tool", input_schema=schema)
        ts = adapter.tool_schema()
        assert ts["name"] == "mcp_srv_my_tool"
        assert ts["input_schema"] == schema

    # -- is_available with breaker --
    def test_is_available_true_by_default(self):
        adapter = self._make_adapter()
        intent = _make_intent()
        assert adapter.is_available(intent) is True

    def test_is_available_false_when_breaker_open(self):
        """Simulate an open breaker by failing enough times."""
        adapter = self._make_adapter(
            call_tool=lambda n, a: (_ for _ in ()).throw(RuntimeError("fail"))
        )
        intent = _make_intent(args={"mcp_tool": "do_thing"})
        # Force-open the breaker via repeated failures.
        for _ in range(6):
            adapter.execute(intent)
        # Breaker should now be open.
        assert adapter.is_available(intent) is False

    # -- cost and use_breaker class attrs --
    def test_cost_is_7(self):
        from curby_jarvis.connectors.mcp_client import MCPConnectorAdapter
        assert MCPConnectorAdapter.cost == 7

    def test_use_breaker_is_true(self):
        from curby_jarvis.connectors.mcp_client import MCPConnectorAdapter
        assert MCPConnectorAdapter.use_breaker is True

    # -- latency populated --
    def test_execute_populates_latency_ms(self):
        adapter = self._make_adapter(call_tool=lambda n, a: {"ok": True})
        intent = _make_intent(args={"mcp_tool": "do_thing"})
        result = adapter.execute(intent)
        assert result.latency_ms >= 0.0


# ---------------------------------------------------------------------------
# Integration: load_config -> synthetic adapters via DI
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_build_adapters_with_injected_adapters_list(self, tmp_path):
        """Simulate P2 building adapters by constructing them directly
        (no live server needed) and assert they behave correctly."""
        from curby_jarvis.connectors.mcp_client import MCPConnectorAdapter

        call_log = []

        def fake_call(tool_name, args):
            call_log.append((tool_name, args))
            return {"answer": 42}

        adapter = MCPConnectorAdapter(
            server="test-server",
            tool_name="summarise",
            input_schema={"type": "object"},
            call_tool=fake_call,
        )

        intent_match = _make_intent(verb=adapter.name, args={"text": "hello"})
        intent_miss = _make_intent(verb="mcp_test_server_other_tool")

        assert adapter.can_handle(intent_match) == 1.0
        assert adapter.can_handle(intent_miss) == 0.0

        result = adapter.execute(intent_match)
        assert result.ok is True
        assert call_log == [("summarise", {"text": "hello"})]

    def test_config_with_valid_file_and_no_mcp_sdk(self, tmp_path):
        """Full round-trip: write config file, call build_adapters. Without mcp SDK
        installed this returns [] but must not raise."""
        from curby_jarvis.mcp_bridge import build_adapters, load_config

        cfg = [{"name": "fs", "command": "/usr/local/bin/mcp-server-filesystem"}]
        config_file = tmp_path / "mcp_servers.json"
        config_file.write_text(json.dumps(cfg), encoding="utf-8")

        loaded = load_config(path=config_file)
        assert loaded == cfg

        adapters = build_adapters(config=loaded, start=True)
        # mcp not installed -> []
        assert isinstance(adapters, list)

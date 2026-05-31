"""MCP bridge — loads Model Context Protocol servers from config and builds Connectors.

WHY this exists: Curby-JARVIS is extensible via MCP servers. A user drops a JSON config
at ~/.curby/mcp_servers.json listing stdio or SSE servers; at startup the router calls
``build_adapters()`` which lazily starts each server via the ``mcp`` Python SDK and
wraps every advertised tool as an MCPConnectorAdapter (cost=7). If the SDK isn't
installed, or a server fails, we log and skip — the rest of the chain continues
unaffected. This module never raises.

Headless contract: top-level imports are stdlib-only. The ``mcp`` SDK is imported
lazily inside ``build_adapters`` so this file loads cleanly in CI.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .connectors import Connector

_LOG = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path("~/.curby/mcp_servers.json")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str | Path | None = None) -> list[dict]:
    """Read ~/.curby/mcp_servers.json and return the server list.

    Returns ``[]`` if the file is absent, unreadable, or malformed — never raises.
    Each item should have at minimum a ``"name"`` key and either ``"command"`` (stdio)
    or ``"url"`` (SSE).  Optional ``"transport"`` and ``"args"`` keys are passed through.
    """
    resolved: Path = Path(path).expanduser() if path else _DEFAULT_CONFIG_PATH.expanduser()
    if not resolved.exists():
        return []
    try:
        raw = resolved.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, list):
            _LOG.warning("mcp_bridge: config %s is not a JSON array — ignoring", resolved)
            return []
        return data
    except Exception as exc:
        _LOG.warning("mcp_bridge: failed to read config %s: %s", resolved, exc)
        return []


# ---------------------------------------------------------------------------
# Adapter builder
# ---------------------------------------------------------------------------

def build_adapters(
    config: list[dict] | None = None,
    *,
    start: bool = True,
) -> "list[Connector]":
    """Build one MCPConnectorAdapter per tool advertised by all configured servers.

    Args:
        config: pre-loaded config list (for tests/DI); if None reads load_config().
        start: when True actually start each server process via the mcp SDK.
                set False in tests to use injected call_tool fakes.

    Returns:
        List of Connector instances. Returns whatever succeeded; if the mcp SDK is
        missing or a server fails, we log + skip. Never raises.
    """
    if config is None:
        config = load_config()
    if not config:
        return []

    # Lazy mcp SDK import — absent in CI; graceful degradation if not installed.
    try:
        import mcp  # noqa: F401
    except ImportError:
        _LOG.info("mcp_bridge: mcp SDK not installed — no MCP adapters loaded")
        return []

    from .connectors.mcp_client import MCPConnectorAdapter

    adapters: list[MCPConnectorAdapter] = []

    for entry in config:
        if not isinstance(entry, dict):
            _LOG.warning("mcp_bridge: skipping non-dict config entry: %r", entry)
            continue
        server_name = entry.get("name")
        if not server_name:
            _LOG.warning("mcp_bridge: config entry missing 'name' — skipping: %r", entry)
            continue

        if not start:
            # DI path for tests: skip SDK session creation; no tools to load without a
            # live server, so callers that need adapters must pass pre-built ones.
            continue

        try:
            tools = _start_server_and_list_tools(entry)
        except Exception as exc:
            _LOG.warning("mcp_bridge: server %r failed to start: %s", server_name, exc)
            continue

        for tool_def in tools:
            try:
                tool_name = tool_def.get("name") or tool_def.get("tool_name", "")
                if not tool_name:
                    continue
                input_schema = tool_def.get("inputSchema") or tool_def.get("input_schema") or {}

                # Build a call_tool closure that captures server_name + the live session.
                call_tool = _make_call_tool(entry)

                adapter = MCPConnectorAdapter(
                    server=server_name,
                    tool_name=tool_name,
                    input_schema=input_schema,
                    call_tool=call_tool,
                )
                adapters.append(adapter)
            except Exception as exc:
                _LOG.warning(
                    "mcp_bridge: failed building adapter for %r/%r: %s",
                    server_name, tool_def, exc,
                )

    return adapters  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Private SDK helpers (lazy, each call is wrapped in try/except)
# ---------------------------------------------------------------------------

def _start_server_and_list_tools(entry: dict) -> list[dict]:
    """Start a single MCP server and synchronously list its tools.

    Uses stdio transport when ``"command"`` is present, SSE when ``"url"`` is present.
    Returns a list of tool definition dicts; raises on failure (caller handles it).
    """
    import asyncio

    command = entry.get("command")
    url = entry.get("url")
    args = entry.get("args") or []

    if command:
        tools = asyncio.run(_list_tools_stdio(command, args))
    elif url:
        tools = asyncio.run(_list_tools_sse(url))
    else:
        raise ValueError("entry must have 'command' or 'url'")

    return tools


async def _list_tools_stdio(command: str, args: list) -> list[dict]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_params = StdioServerParameters(command=command, args=args, env=None)
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [
                {
                    "name": t.name,
                    "description": getattr(t, "description", ""),
                    "inputSchema": getattr(t, "inputSchema", {}),
                }
                for t in result.tools
            ]


async def _list_tools_sse(url: str) -> list[dict]:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [
                {
                    "name": t.name,
                    "description": getattr(t, "description", ""),
                    "inputSchema": getattr(t, "inputSchema", {}),
                }
                for t in result.tools
            ]


def _make_call_tool(entry: dict):
    """Return a synchronous call_tool(tool_name, args) -> dict closure for a server."""
    import asyncio

    command = entry.get("command")
    url = entry.get("url")
    args_extra = entry.get("args") or []

    def call_tool(tool_name: str, args: dict) -> dict:
        try:
            if command:
                return asyncio.run(_call_tool_stdio(command, args_extra, tool_name, args))
            elif url:
                return asyncio.run(_call_tool_sse(url, tool_name, args))
            else:
                return {"error": "no command or url in server config"}
        except Exception as exc:
            return {"error": repr(exc)}

    return call_tool


async def _call_tool_stdio(command: str, extra_args: list, tool_name: str, args: dict) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_params = StdioServerParameters(command=command, args=extra_args, env=None)
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, args)
            return {"content": [getattr(c, "text", str(c)) for c in result.content]}


async def _call_tool_sse(url: str, tool_name: str, args: dict) -> dict:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, args)
            return {"content": [getattr(c, "text", str(c)) for c in result.content]}


__all__ = ["load_config", "build_adapters"]

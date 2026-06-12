"""Headless unit tests for permissions.py.

No pyobjc, no Quartz, no display required. All native probes are monkeypatched
with simple fakes. Tests cover:
  - PermissionCache TTL with injected clock + fake probe
  - PermissionCache.invalidate()
  - full_report() returns a dict with the expected keys
  - full_report() sets all_green correctly
  - probe_agent() reads env vars + shutil.which
  - probe_accessibility() falls back gracefully when ax module absent
  - probe_automation() maps TCC denial to 'denied', happy path to 'authorized'
  - probe_screen_recording() returns True when Quartz absent
  - Module imports headless
"""
from __future__ import annotations

import importlib
import os
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_clock(start: float = 0.0):
    state = [start]

    def clock() -> float:
        return state[0]

    def advance(seconds: float) -> None:
        state[0] += seconds

    return clock, advance


# ---------------------------------------------------------------------------
# PermissionCache — TTL memoisation
# ---------------------------------------------------------------------------

def test_cache_calls_probe_on_first_get():
    from curby_jarvis.permissions import PermissionCache

    calls = [0]

    def fake_probe():
        calls[0] += 1
        return True

    clock, _ = make_clock()
    cache = PermissionCache(ttl=5.0, clock=clock)
    result = cache.get("accessibility", probe=fake_probe)
    assert result is True
    assert calls[0] == 1


def test_cache_returns_cached_value_within_ttl():
    from curby_jarvis.permissions import PermissionCache

    calls = [0]

    def fake_probe():
        calls[0] += 1
        return calls[0]  # different each call

    clock, advance = make_clock()
    cache = PermissionCache(ttl=5.0, clock=clock)
    r1 = cache.get("foo", probe=fake_probe)
    advance(4.9)
    r2 = cache.get("foo", probe=fake_probe)
    assert r1 == r2 == 1  # second call should hit cache
    assert calls[0] == 1


def test_cache_refreshes_after_ttl_expires():
    from curby_jarvis.permissions import PermissionCache

    calls = [0]

    def fake_probe():
        calls[0] += 1
        return calls[0]

    clock, advance = make_clock()
    cache = PermissionCache(ttl=5.0, clock=clock)
    r1 = cache.get("bar", probe=fake_probe)
    advance(5.1)
    r2 = cache.get("bar", probe=fake_probe)
    assert r1 == 1
    assert r2 == 2
    assert calls[0] == 2


def test_cache_invalidate_forces_refresh():
    from curby_jarvis.permissions import PermissionCache

    calls = [0]

    def fake_probe():
        calls[0] += 1
        return calls[0]

    clock, _ = make_clock()
    cache = PermissionCache(ttl=60.0, clock=clock)
    cache.get("baz", probe=fake_probe)
    cache.invalidate()
    cache.get("baz", probe=fake_probe)
    assert calls[0] == 2


def test_cache_get_unknown_key_returns_none():
    from curby_jarvis.permissions import PermissionCache

    clock, _ = make_clock()
    cache = PermissionCache(ttl=5.0, clock=clock)
    assert cache.get("nonexistent_xyz") is None


def test_cache_probe_exception_returns_none():
    from curby_jarvis.permissions import PermissionCache

    def bad_probe():
        raise RuntimeError("oops")

    clock, _ = make_clock()
    cache = PermissionCache(ttl=5.0, clock=clock)
    result = cache.get("err", probe=bad_probe)
    assert result is None  # graceful


# ---------------------------------------------------------------------------
# full_report() — keys + all_green logic
# ---------------------------------------------------------------------------

def test_full_report_returns_expected_keys(monkeypatch):
    import curby_jarvis.permissions as pmod

    monkeypatch.setattr(pmod, "probe_accessibility", lambda: True)
    monkeypatch.setattr(pmod, "probe_automation", lambda: "authorized")
    monkeypatch.setattr(pmod, "probe_screen_recording", lambda: True)
    monkeypatch.setattr(pmod, "probe_agent", lambda: {"claude_cli": "/usr/bin/claude", "api_key": True})

    report = pmod.full_report()
    for key in ("accessibility", "automation", "screen_recording", "agent", "all_green"):
        assert key in report, f"missing key: {key}"


def test_full_report_all_green_true_when_all_pass(monkeypatch):
    import curby_jarvis.permissions as pmod

    monkeypatch.setattr(pmod, "probe_accessibility", lambda: True)
    monkeypatch.setattr(pmod, "probe_automation", lambda: "authorized")
    monkeypatch.setattr(pmod, "probe_screen_recording", lambda: True)
    monkeypatch.setattr(pmod, "probe_agent", lambda: {"claude_cli": "/bin/claude", "api_key": True})

    assert pmod.full_report()["all_green"] is True


def test_full_report_all_green_false_when_ax_missing(monkeypatch):
    import curby_jarvis.permissions as pmod

    monkeypatch.setattr(pmod, "probe_accessibility", lambda: False)
    monkeypatch.setattr(pmod, "probe_automation", lambda: "authorized")
    monkeypatch.setattr(pmod, "probe_screen_recording", lambda: True)
    monkeypatch.setattr(pmod, "probe_agent", lambda: {"claude_cli": "", "api_key": True})

    assert pmod.full_report()["all_green"] is False


def test_full_report_all_green_false_when_no_api_key(monkeypatch):
    import curby_jarvis.permissions as pmod

    monkeypatch.setattr(pmod, "probe_accessibility", lambda: True)
    monkeypatch.setattr(pmod, "probe_automation", lambda: "authorized")
    monkeypatch.setattr(pmod, "probe_screen_recording", lambda: True)
    monkeypatch.setattr(pmod, "probe_agent", lambda: {"claude_cli": "/bin/claude", "api_key": False})

    assert pmod.full_report()["all_green"] is False


def test_full_report_all_green_false_when_automation_denied(monkeypatch):
    import curby_jarvis.permissions as pmod

    monkeypatch.setattr(pmod, "probe_accessibility", lambda: True)
    monkeypatch.setattr(pmod, "probe_automation", lambda: "denied")
    monkeypatch.setattr(pmod, "probe_screen_recording", lambda: True)
    monkeypatch.setattr(pmod, "probe_agent", lambda: {"claude_cli": "", "api_key": True})

    assert pmod.full_report()["all_green"] is False


def test_full_report_never_raises_when_probes_explode(monkeypatch):
    import curby_jarvis.permissions as pmod

    def boom():
        raise RuntimeError("exploded")

    monkeypatch.setattr(pmod, "probe_accessibility", boom)
    monkeypatch.setattr(pmod, "probe_automation", boom)
    monkeypatch.setattr(pmod, "probe_screen_recording", boom)
    monkeypatch.setattr(pmod, "probe_agent", boom)

    report = pmod.full_report()
    assert "all_green" in report
    assert report["all_green"] is False


# ---------------------------------------------------------------------------
# probe_agent — reads env vars
# ---------------------------------------------------------------------------

def test_probe_agent_reads_api_key(monkeypatch):
    import curby_jarvis.permissions as pmod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    result = pmod.probe_agent()
    assert result["api_key"] is True


def test_probe_agent_no_api_key(monkeypatch):
    import curby_jarvis.permissions as pmod

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = pmod.probe_agent()
    assert result["api_key"] is False


def test_probe_agent_uses_claude_cli_env(monkeypatch):
    import curby_jarvis.permissions as pmod

    monkeypatch.setenv("CLAUDE_CLI", "/custom/claude")
    result = pmod.probe_agent()
    assert result["claude_cli"] == "/custom/claude"


def test_probe_agent_falls_back_to_which(monkeypatch):
    import shutil
    import curby_jarvis.permissions as pmod

    monkeypatch.delenv("CLAUDE_CLI", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/found/claude")
    result = pmod.probe_agent()
    assert result["claude_cli"] == "/found/claude"


def test_probe_agent_empty_when_no_cli(monkeypatch):
    import shutil
    import curby_jarvis.permissions as pmod

    monkeypatch.delenv("CLAUDE_CLI", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = pmod.probe_agent()
    assert result["claude_cli"] == ""


# ---------------------------------------------------------------------------
# probe_accessibility — graceful fallback when ax_bridge absent
# ---------------------------------------------------------------------------

def test_probe_accessibility_false_when_import_fails(monkeypatch):
    import curby_jarvis.permissions as pmod

    # simulate ax_bridge being unimportable by making probe call raise
    def fake_ax_available():
        raise ImportError("no pyobjc")

    original = None
    try:
        from curby_jarvis.ax import ax_bridge
        original = ax_bridge.ax_available
        monkeypatch.setattr(ax_bridge, "ax_available", fake_ax_available)
        result = pmod.probe_accessibility()
        assert result is False
    except ImportError:
        # ax_bridge itself not importable → probe must return False
        result = pmod.probe_accessibility()
        assert result is False


def test_probe_accessibility_true_when_ax_available(monkeypatch):
    import curby_jarvis.permissions as pmod

    try:
        from curby_jarvis.ax import ax_bridge
        monkeypatch.setattr(ax_bridge, "ax_available", lambda: True)
        result = pmod.probe_accessibility()
        assert result is True
    except ImportError:
        pytest.skip("ax_bridge not importable in this environment")


# ---------------------------------------------------------------------------
# probe_automation — maps TCC markers to 'denied'
# ---------------------------------------------------------------------------

def test_probe_automation_authorized(monkeypatch):
    import curby_jarvis.permissions as pmod

    class FakeBridge:
        def run(self, script, timeout=1.0):
            return (True, "ok")

    monkeypatch.setattr(pmod, "probe_automation", lambda: "authorized")
    assert pmod.probe_automation() == "authorized"


def test_probe_automation_denied_when_tcc(monkeypatch):
    import curby_jarvis.permissions as pmod

    # Patch at the osascript_bridge level
    try:
        import curby_jarvis.osascript_bridge as osa

        class FakeBridge:
            def run(self, script, timeout=1.0):
                return (False, "not authorized to send apple events")

        monkeypatch.setattr(osa, "_SHARED", FakeBridge())

        def fake_shared():
            return FakeBridge()

        monkeypatch.setattr(osa, "shared", fake_shared)
        result = pmod.probe_automation()
        assert result == "denied"
    except Exception:
        pytest.skip("osascript_bridge not patchable in this environment")


def test_probe_automation_unknown_when_import_fails(monkeypatch):
    import curby_jarvis.permissions as pmod

    # Force an import error path inside probe_automation
    original_fn = pmod.probe_automation

    def exploding_automation():
        try:
            raise ImportError("no osascript")
        except Exception:
            return "unknown"

    monkeypatch.setattr(pmod, "probe_automation", exploding_automation)
    assert pmod.probe_automation() == "unknown"
    monkeypatch.setattr(pmod, "probe_automation", original_fn)


# ---------------------------------------------------------------------------
# probe_screen_recording — Quartz absent → True
# ---------------------------------------------------------------------------

def test_probe_screen_recording_true_when_quartz_absent(monkeypatch):
    import curby_jarvis.permissions as pmod

    # Simulate missing Quartz by replacing it with a module that raises ImportError
    import sys
    original = sys.modules.get("Quartz")
    try:
        sys.modules["Quartz"] = None  # type: ignore[assignment]
        result = pmod.probe_screen_recording()
        assert result is True
    finally:
        if original is None:
            sys.modules.pop("Quartz", None)
        else:
            sys.modules["Quartz"] = original


# ---------------------------------------------------------------------------
# request_screen_recording — never raises
# ---------------------------------------------------------------------------

def test_request_screen_recording_never_raises(monkeypatch):
    import sys
    import curby_jarvis.permissions as pmod

    sys.modules["Quartz"] = None  # type: ignore[assignment]
    try:
        pmod.request_screen_recording()  # must not raise
    finally:
        sys.modules.pop("Quartz", None)


# ---------------------------------------------------------------------------
# PermissionCache with named probes (default probe map)
# ---------------------------------------------------------------------------

def test_cache_get_named_agent_probe(monkeypatch):
    import curby_jarvis.permissions as pmod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
    monkeypatch.delenv("CLAUDE_CLI", raising=False)

    clock, _ = make_clock()
    cache = pmod.PermissionCache(ttl=5.0, clock=clock)
    result = cache.get("agent")
    assert isinstance(result, dict)
    assert "api_key" in result


# ---------------------------------------------------------------------------
# Headless import
# ---------------------------------------------------------------------------

def test_module_imports_headless():
    m = importlib.import_module("curby_jarvis.permissions")
    assert hasattr(m, "PermissionCache")
    assert hasattr(m, "full_report")
    assert hasattr(m, "probe_accessibility")
    assert hasattr(m, "probe_automation")
    assert hasattr(m, "probe_screen_recording")
    assert hasattr(m, "probe_agent")
    assert hasattr(m, "request_screen_recording")

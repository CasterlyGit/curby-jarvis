"""Headless tests for browser_tab + osascript_bridge.

No display, no camera, no Automation grant, no network. cgevent and the front-app
AX lookup are mocked; the OsascriptBridge is faked so we assert the AppleScript
WITHOUT ever spawning osascript. The fresh-subprocess path is exercised only via a
fake subprocess module to prove timeout/TCC/exit mapping without touching the OS.
"""
from __future__ import annotations

import sys
import types

import pytest

from curby_jarvis.connectors.browser_tab import (
    BrowserTabConnector,
    _KEY_NEXT,
    _KEY_PREV,
    _browser_kind,
)
from curby_jarvis.intent import Intent
from curby_jarvis.osascript_bridge import OsascriptBridge, looks_like_tcc_denial


# --- fakes -------------------------------------------------------------------

class FakeBridge:
    """Records the last script + returns a scripted (ok, out)."""

    def __init__(self, result=(True, "1")):
        self.result = result
        self.calls = []

    def run(self, script, timeout=1.0):
        self.calls.append((script, timeout))
        return self.result


def _connector(front="Google Chrome", bridge=None):
    c = BrowserTabConnector(bridge=bridge or FakeBridge())
    # Pin the front app so no AX permission is needed in CI.
    c._front_app_name = lambda: front
    return c


def _install_fake_cgevent(monkeypatch, recorder):
    """Install a fake curby_jarvis.cgevent whose key() records the combo."""
    mod = types.ModuleType("curby_jarvis.cgevent")

    def key(combo):
        recorder.append(combo)
        return True

    mod.key = key
    monkeypatch.setitem(sys.modules, "curby_jarvis.cgevent", mod)
    # `from .. import cgevent` resolves via the parent-package ATTRIBUTE once the
    # real submodule (now on disk) has been imported anywhere, so setitem alone
    # is insufficient; rebind the attr too. monkeypatch restores both on teardown.
    import curby_jarvis
    monkeypatch.setattr(curby_jarvis, "cgevent", mod, raising=False)
    return mod


@pytest.fixture(autouse=True)
def _no_secure_input(monkeypatch):
    # Force Secure Input OFF so switch_tab is available + executes.
    monkeypatch.setattr(
        "curby_jarvis.ax.secure_input.secure_input_active", lambda: False
    )


# --- switch_tab via CGEvent (zero TCC) ---------------------------------------

def test_switch_next_uses_right_combo(monkeypatch):
    keys = []
    _install_fake_cgevent(monkeypatch, keys)
    c = _connector()
    res = c.execute(Intent("switch_tab", args={"dir": "next"}))
    assert res.ok is True
    assert keys == [_KEY_NEXT] == ["cmd+shift+]"]


def test_switch_prev_uses_right_combo(monkeypatch):
    keys = []
    _install_fake_cgevent(monkeypatch, keys)
    c = _connector()
    res = c.execute(Intent("switch_tab", args={"dir": "prev"}))
    assert res.ok is True
    assert keys == [_KEY_PREV] == ["cmd+shift+["]


def test_switch_tab_default_dir_is_next(monkeypatch):
    keys = []
    _install_fake_cgevent(monkeypatch, keys)
    c = _connector()
    res = c.execute(Intent("switch_tab"))  # no dir -> default next
    assert res.ok and keys == ["cmd+shift+]"]


def test_switch_tab_cgevent_blocked_returns_not_ok(monkeypatch):
    mod = types.ModuleType("curby_jarvis.cgevent")
    mod.key = lambda combo: False  # CGEvent refused (e.g. blocked)
    monkeypatch.setitem(sys.modules, "curby_jarvis.cgevent", mod)
    import curby_jarvis
    monkeypatch.setattr(curby_jarvis, "cgevent", mod, raising=False)
    c = _connector()
    res = c.execute(Intent("switch_tab", args={"dir": "next"}))
    assert res.ok is False and res.error == "cgevent_blocked"


def test_switch_tab_secure_input_blocks(monkeypatch):
    monkeypatch.setattr(
        "curby_jarvis.ax.secure_input.secure_input_active", lambda: True
    )
    c = _connector()
    # available() should be False, and execute() should hard-block.
    assert c.is_available(Intent("switch_tab")) is False
    res = c.execute(Intent("switch_tab", args={"dir": "next"}))
    assert res.ok is False and res.error == "secure_input_blocked"


def test_switch_tab_can_handle_and_cost():
    c = _connector()
    assert c.cost == 6
    assert c.can_handle(Intent("switch_tab")) > 0.9
    assert c.can_handle(Intent("tab_by_name", target="x")) > 0.0
    assert c.can_handle(Intent("open", target="Safari")) == 0.0


# --- tab_by_name via osascript (Chromium) ------------------------------------

def test_tab_by_name_builds_plausible_chromium_script():
    bridge = FakeBridge(result=(True, "3"))
    c = _connector(front="Google Chrome", bridge=bridge)
    res = c.execute(Intent("tab_by_name", target="Gmail", args={"name": "Gmail"}))
    assert res.ok is True
    assert len(bridge.calls) == 1
    script, timeout = bridge.calls[0]
    assert timeout == 1.0
    assert 'tell application "Google Chrome"' in script
    assert "tabs of front window" in script
    assert 'contains "Gmail"' in script
    assert "set active tab index of front window" in script


def test_tab_by_name_safari_uses_current_tab():
    bridge = FakeBridge(result=(True, "2"))
    c = _connector(front="Safari", bridge=bridge)
    res = c.execute(Intent("tab_by_name", target="Docs"))
    assert res.ok is True
    script, _ = bridge.calls[0]
    assert 'tell application "Safari"' in script
    assert "set current tab of front window" in script
    assert 'contains "Docs"' in script


def test_tab_by_name_not_found_returns_not_ok():
    bridge = FakeBridge(result=(True, "0"))  # script ran, matched nothing
    c = _connector(front="Arc", bridge=bridge)
    res = c.execute(Intent("tab_by_name", target="Nope"))
    assert res.ok is False and res.error == "tab_not_found"


def test_tab_by_name_quotes_are_escaped():
    bridge = FakeBridge(result=(True, "1"))
    c = _connector(front="Brave Browser", bridge=bridge)
    c.execute(Intent("tab_by_name", target='a"b'))
    script, _ = bridge.calls[0]
    # the embedded quote must be backslash-escaped so the AppleScript stays valid
    assert '\\"' in script


def test_automation_denied_degrades_gracefully():
    bridge = FakeBridge(result=(False, "automation_denied"))
    c = _connector(front="Google Chrome", bridge=bridge)
    res = c.execute(Intent("tab_by_name", target="Gmail"))
    assert res.ok is False and res.error == "automation_denied"


def test_non_browser_front_app_misses_cleanly():
    bridge = FakeBridge()
    c = _connector(front="TextEdit", bridge=bridge)
    res = c.execute(Intent("tab_by_name", target="Gmail"))
    assert res.ok is False and res.error == "not_a_browser"
    assert bridge.calls == []  # never even built a script


def test_tab_by_name_no_name_returns_not_ok():
    bridge = FakeBridge()
    c = _connector(front="Google Chrome", bridge=bridge)
    res = c.execute(Intent("tab_by_name", target=""))
    assert res.ok is False and res.error == "no_tab_name"


# --- goto_tab (numeric index) ------------------------------------------------

def test_goto_tab_chromium_sets_index():
    bridge = FakeBridge(result=(True, ""))
    c = _connector(front="Google Chrome", bridge=bridge)
    res = c.execute(Intent("goto_tab", args={"index": 3}))
    assert res.ok is True
    script, _ = bridge.calls[0]
    assert "set active tab index of front window to 3" in script


def test_goto_tab_safari_selects_tab():
    bridge = FakeBridge(result=(True, ""))
    c = _connector(front="Safari", bridge=bridge)
    res = c.execute(Intent("goto_tab", args={"index": 2}))
    assert res.ok is True
    script, _ = bridge.calls[0]
    assert "set current tab of front window to tab 2" in script


def test_goto_tab_index_from_numeric_target():
    bridge = FakeBridge(result=(True, ""))
    c = _connector(front="Arc", bridge=bridge)
    res = c.execute(Intent("goto_tab", target="5"))
    assert res.ok is True
    assert "to 5" in bridge.calls[0][0]


def test_goto_tab_missing_index_returns_not_ok():
    bridge = FakeBridge()
    c = _connector(front="Google Chrome", bridge=bridge)
    res = c.execute(Intent("goto_tab"))
    assert res.ok is False and res.error == "no_tab_index"


# --- preview is side-effect free ---------------------------------------------

def test_preview_switch_tab_has_combo_literal():
    c = _connector()
    card = c.preview(Intent("switch_tab", args={"dir": "prev"}))
    assert card.literal == "cmd+shift+["
    assert card.mechanism == "browser_tab"


def test_preview_tab_by_name_mentions_name():
    bridge = FakeBridge()
    c = _connector(front="Google Chrome", bridge=bridge)
    card = c.preview(Intent("tab_by_name", target="Gmail"))
    assert "Gmail" in card.literal
    assert "Chrome" in card.gloss
    # preview must NOT have invoked osascript
    assert bridge.calls == []


def test_execute_never_raises_on_bad_bridge():
    class Boom:
        def run(self, *a, **k):
            raise RuntimeError("kaboom")

    c = _connector(front="Google Chrome", bridge=Boom())
    res = c.execute(Intent("tab_by_name", target="Gmail"))
    assert res.ok is False and res.error == "exception"


# --- browser classification --------------------------------------------------

@pytest.mark.parametrize("name,kind", [
    ("Google Chrome", "chromium"),
    ("Arc", "chromium"),
    ("Brave Browser", "chromium"),
    ("Microsoft Edge", "chromium"),
    ("Safari", "safari"),
    ("Safari Technology Preview", "safari"),
    ("TextEdit", ""),
    ("", ""),
])
def test_browser_kind(name, kind):
    assert _browser_kind(name) == kind


# --- OsascriptBridge unit (fake subprocess, no real osascript) ---------------

class _FakeProc:
    def __init__(self, out="ok", err="", rc=0, hang=False):
        self._out, self._err, self.returncode = out, err, rc
        self._hang = hang
        self.killed = False

    def communicate(self):
        if self._hang:
            import time
            time.sleep(5)  # simulate a wedged child; watchdog must kill us
        return self._out, self._err

    def kill(self):
        self.killed = True


def _fake_subprocess(proc):
    mod = types.ModuleType("subprocess")
    mod.PIPE = -1

    def Popen(args, stdout=None, stderr=None, text=None):
        proc.args = args
        return proc

    mod.Popen = Popen
    return mod


def test_bridge_run_success(monkeypatch):
    proc = _FakeProc(out="  hello \n", rc=0)
    monkeypatch.setitem(sys.modules, "subprocess", _fake_subprocess(proc))
    ok, out = OsascriptBridge().run('tell app "Finder" to activate')
    assert ok is True and out == "hello"
    assert proc.args[0] == "osascript" and proc.args[1] == "-e"


def test_bridge_run_tcc_denial(monkeypatch):
    proc = _FakeProc(out="", err="Not authorized to send Apple events (-1743).", rc=1)
    monkeypatch.setitem(sys.modules, "subprocess", _fake_subprocess(proc))
    ok, out = OsascriptBridge().run("tell app \"Chrome\" to count windows")
    assert ok is False and out == "automation_denied"


def test_bridge_run_timeout_kills_child(monkeypatch):
    proc = _FakeProc(hang=True)
    monkeypatch.setitem(sys.modules, "subprocess", _fake_subprocess(proc))
    ok, out = OsascriptBridge().run("delay 10", timeout=0.2)
    assert ok is False and out == "osascript_timeout"
    assert proc.killed is True


def test_bridge_run_generic_failure(monkeypatch):
    proc = _FakeProc(out="", err="some other error", rc=2)
    monkeypatch.setitem(sys.modules, "subprocess", _fake_subprocess(proc))
    ok, out = OsascriptBridge().run("boom")
    assert ok is False and out == "some other error"


def test_bridge_empty_script():
    ok, out = OsascriptBridge().run("")
    assert ok is False and out == "empty_script"


def test_looks_like_tcc_denial():
    assert looks_like_tcc_denial("Not authorized to send Apple events")
    assert looks_like_tcc_denial("error -1743")
    assert not looks_like_tcc_denial("syntax error: expected end of line")


def test_bridge_start_stop_are_noops():
    b = OsascriptBridge()
    assert b.start() is b
    assert b.stop() is None

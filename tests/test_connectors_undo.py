"""Headless undo_fn tests for the three M-owned connectors.

Verifies:
- BrowserTabConnector: switch_tab undo returns opposite combo; goto_tab/tab_by_name
  undo restores the captured previous index; irreversible paths => undo_fn is None.
- AppLaunchConnector: open/run undo invokes _hide_app; search/mail => undo_fn None.
- DeixisClickConnector: all verbs (click_at, move, drag) => undo_fn is None.

No display, no OS calls, no permissions: all native seams are monkeypatched.
"""
from __future__ import annotations

import sys
import types

import pytest

from curby_jarvis.connectors.browser_tab import BrowserTabConnector, _KEY_NEXT, _KEY_PREV
from curby_jarvis.connectors.app_launch import AppLaunchConnector
from curby_jarvis.connectors.deixis_click import DeixisClickConnector
from curby_jarvis.intent import Intent


# ============================================================================
# Shared helpers / fixtures
# ============================================================================

FAKE_INDEX = {
    "spotify": "/Applications/Spotify.app",
    "safari": "/Applications/Safari.app",
}


@pytest.fixture(autouse=True)
def _no_secure_input(monkeypatch):
    """Turn off Secure Input globally so cgevent paths are reachable."""
    monkeypatch.setattr(
        "curby_jarvis.ax.secure_input.secure_input_active", lambda: False
    )


def _install_fake_cgevent(monkeypatch, recorder=None):
    """Return a fake cgevent module where key() records calls and succeeds."""
    mod = types.ModuleType("curby_jarvis.cgevent")
    calls = recorder if recorder is not None else []

    def key(combo):
        calls.append(combo)
        return True

    mod.key = key
    monkeypatch.setitem(sys.modules, "curby_jarvis.cgevent", mod)
    import curby_jarvis
    monkeypatch.setattr(curby_jarvis, "cgevent", mod, raising=False)
    return mod, calls


class FakeBridge:
    """Records scripts + returns scripted (ok, out)."""

    def __init__(self, result=(True, "1")):
        self.result = result
        self.calls = []

    def run(self, script, timeout=1.0):
        self.calls.append((script, timeout))
        return self.result


def _browser_connector(front="Google Chrome", bridge=None):
    c = BrowserTabConnector(bridge=bridge or FakeBridge())
    c._front_app_name = lambda: front
    return c


# ============================================================================
# BrowserTabConnector undo tests
# ============================================================================

class TestBrowserTabUndo:

    def test_switch_tab_next_undo_is_prev_combo(self, monkeypatch):
        _, keys = _install_fake_cgevent(monkeypatch)
        c = _browser_connector()
        res = c.execute(Intent("switch_tab", args={"dir": "next"}))
        assert res.ok is True
        assert res.undo_fn is not None
        # Execute undo: should send the reverse combo.
        undo_keys = []
        _install_fake_cgevent(monkeypatch, undo_keys)
        result = res.undo_fn()
        assert result is True
        assert _KEY_PREV in undo_keys

    def test_switch_tab_prev_undo_is_next_combo(self, monkeypatch):
        _, keys = _install_fake_cgevent(monkeypatch)
        c = _browser_connector()
        res = c.execute(Intent("switch_tab", args={"dir": "prev"}))
        assert res.ok is True
        assert res.undo_fn is not None
        undo_keys = []
        _install_fake_cgevent(monkeypatch, undo_keys)
        res.undo_fn()
        assert _KEY_NEXT in undo_keys

    def test_switch_tab_failed_exec_has_no_undo(self, monkeypatch):
        """When cgevent.key returns False, undo_fn should be None."""
        mod = types.ModuleType("curby_jarvis.cgevent")
        mod.key = lambda combo: False
        monkeypatch.setitem(sys.modules, "curby_jarvis.cgevent", mod)
        import curby_jarvis
        monkeypatch.setattr(curby_jarvis, "cgevent", mod, raising=False)
        c = _browser_connector()
        res = c.execute(Intent("switch_tab", args={"dir": "next"}))
        assert res.ok is False
        assert res.undo_fn is None

    def test_switch_tab_undo_never_raises(self, monkeypatch):
        """undo_fn must not raise even if cgevent import fails."""
        _, _ = _install_fake_cgevent(monkeypatch)
        c = _browser_connector()
        res = c.execute(Intent("switch_tab", args={"dir": "next"}))
        assert res.undo_fn is not None
        # Remove the fake cgevent to simulate an import error.
        monkeypatch.delitem(sys.modules, "curby_jarvis.cgevent", raising=False)
        import curby_jarvis
        monkeypatch.setattr(curby_jarvis, "cgevent", None, raising=False)
        # Must not raise:
        result = res.undo_fn()
        # Returns False gracefully when cgevent unavailable.
        assert isinstance(result, bool)

    def test_goto_tab_undo_restores_previous_index(self, monkeypatch):
        """goto_tab undo should issue a script going back to the captured tab."""
        # First call (index query): returns "2"; second call (navigate): succeeds.
        bridge = FakeBridge(result=(True, "2"))
        c = _browser_connector(front="Google Chrome", bridge=bridge)
        res = c.execute(Intent("goto_tab", args={"index": 5}))
        assert res.ok is True
        assert res.undo_fn is not None
        # Now execute undo: should run a script navigating to index 2.
        bridge.calls.clear()
        result = res.undo_fn()
        assert result is True
        assert len(bridge.calls) >= 1
        undo_script = bridge.calls[0][0]
        assert "set active tab index of front window to 2" in undo_script

    def test_goto_tab_no_prev_index_has_no_undo(self, monkeypatch):
        """If the pre-navigate index query fails, undo_fn should be None."""
        # First bridge call returns failure (can't get current index).
        call_count = {"n": 0}

        class FlipBridge:
            def run(self, script, timeout=1.0):
                call_count["n"] += 1
                # First call = current-index query -> fail; second = navigate -> ok.
                if call_count["n"] == 1:
                    return (False, "osascript_timeout")
                return (True, "")

        c = _browser_connector(front="Google Chrome", bridge=FlipBridge())
        res = c.execute(Intent("goto_tab", args={"index": 3}))
        assert res.ok is True
        assert res.undo_fn is None  # couldn't capture previous index

    def test_tab_by_name_undo_restores_previous_index_safari(self, monkeypatch):
        """tab_by_name on Safari: undo restores original tab via Safari goto script."""
        # Pre-navigate index query returns "1"; navigation returns "3".
        call_count = {"n": 0}

        class SeqBridge:
            def run(self, script, timeout=1.0):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return (True, "1")  # current index
                return (True, "3")     # successful nav

            calls = []

        bridge = SeqBridge()
        c = _browser_connector(front="Safari", bridge=bridge)
        res = c.execute(Intent("tab_by_name", target="Docs"))
        assert res.ok is True
        assert res.undo_fn is not None

        # Undo should run a safari goto-index-1 script.
        undo_calls = []
        orig_run = bridge.run

        def recording_run(script, timeout=1.0):
            undo_calls.append(script)
            return (True, "")

        bridge.run = recording_run
        result = res.undo_fn()
        assert result is True
        assert undo_calls, "undo_fn must invoke the bridge"
        assert "Safari" in undo_calls[0]
        assert "tab 1 of front window" in undo_calls[0]

    def test_tab_by_name_not_found_has_no_undo(self, monkeypatch):
        """When tab_by_name returns tab_not_found, undo_fn must be None."""
        # First call (index): ok; second call (search): returns "0" (not found).
        call_count = {"n": 0}

        class ZeroBridge:
            def run(self, script, timeout=1.0):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return (True, "2")
                return (True, "0")  # no match

        c = _browser_connector(front="Google Chrome", bridge=ZeroBridge())
        res = c.execute(Intent("tab_by_name", target="Nowhere"))
        assert res.ok is False and res.error == "tab_not_found"
        assert res.undo_fn is None

    def test_not_a_browser_has_no_undo(self):
        c = _browser_connector(front="TextEdit")
        res = c.execute(Intent("tab_by_name", target="Gmail"))
        assert res.ok is False
        assert res.undo_fn is None

    def test_undo_fn_return_type_is_bool(self, monkeypatch):
        """undo_fn must return bool, not None or other type."""
        _, _ = _install_fake_cgevent(monkeypatch)
        c = _browser_connector()
        res = c.execute(Intent("switch_tab", args={"dir": "next"}))
        assert res.undo_fn is not None
        result = res.undo_fn()
        assert isinstance(result, bool)


# ============================================================================
# AppLaunchConnector undo tests
# ============================================================================

class TestAppLaunchUndo:

    def test_open_resolves_undo_fn(self, monkeypatch):
        """Successful open/run populates undo_fn."""
        c = AppLaunchConnector(app_index=FAKE_INDEX)

        def fake_launch(intent):
            return True, "/Applications/Spotify.app"

        monkeypatch.setattr(c, "_launch_app", fake_launch)
        res = c.execute(Intent("open", target="spotify"))
        assert res.ok is True
        assert res.undo_fn is not None

    def test_open_undo_calls_hide(self, monkeypatch):
        """undo_fn for open/run calls _hide_app with the bundle path."""
        c = AppLaunchConnector(app_index=FAKE_INDEX)
        hidden = {}

        def fake_launch(intent):
            return True, "/Applications/Spotify.app"

        def fake_hide(path):
            hidden["path"] = path
            return True

        monkeypatch.setattr(c, "_launch_app", fake_launch)
        monkeypatch.setattr(c, "_hide_app", fake_hide)
        res = c.execute(Intent("open", target="spotify"))
        assert res.undo_fn is not None
        result = res.undo_fn()
        assert result is True
        assert hidden["path"] == "/Applications/Spotify.app"

    def test_open_failed_has_no_undo(self, monkeypatch):
        """When launch fails, undo_fn must be None."""
        c = AppLaunchConnector(app_index=FAKE_INDEX)

        def fake_launch(intent):
            return False, "app_not_found:frobnicate"

        monkeypatch.setattr(c, "_launch_app", fake_launch)
        res = c.execute(Intent("open", target="frobnicate"))
        assert res.ok is False
        assert res.undo_fn is None

    def test_search_has_no_undo(self, monkeypatch):
        """search opens a URL — irreversible, undo_fn must be None."""
        c = AppLaunchConnector(app_index=FAKE_INDEX)

        def fake_open(url):
            return True, url

        monkeypatch.setattr(c, "_open_url", fake_open)
        res = c.execute(Intent("search", target="cats", args={"query": "cats"}))
        assert res.ok is True
        assert res.undo_fn is None

    def test_mail_has_no_undo(self, monkeypatch):
        """mail opens a mailto URL — irreversible, undo_fn must be None."""
        c = AppLaunchConnector(app_index=FAKE_INDEX)

        def fake_open(url):
            return True, url

        monkeypatch.setattr(c, "_open_url", fake_open)
        res = c.execute(Intent("mail", target="a@b.com"))
        assert res.ok is True
        assert res.undo_fn is None

    def test_open_undo_never_raises_when_hide_explodes(self, monkeypatch):
        """undo_fn must not raise even if _hide_app throws."""
        c = AppLaunchConnector(app_index=FAKE_INDEX)

        def fake_launch(intent):
            return True, "/Applications/Spotify.app"

        def boom(path):
            raise RuntimeError("NSWorkspace wedged")

        monkeypatch.setattr(c, "_launch_app", fake_launch)
        monkeypatch.setattr(c, "_hide_app", boom)
        res = c.execute(Intent("open", target="spotify"))
        assert res.undo_fn is not None
        # Must not raise; returns False gracefully.
        result = res.undo_fn()
        assert result is False

    def test_run_verb_also_gets_undo(self, monkeypatch):
        """'run' verb behaves identically to 'open' for undo purposes."""
        c = AppLaunchConnector(app_index=FAKE_INDEX)

        def fake_launch(intent):
            return True, "/Applications/Spotify.app"

        monkeypatch.setattr(c, "_launch_app", fake_launch)
        res = c.execute(Intent("run", target="spotify"))
        assert res.ok is True
        assert res.undo_fn is not None

    def test_undo_fn_return_type_is_bool(self, monkeypatch):
        c = AppLaunchConnector(app_index=FAKE_INDEX)

        def fake_launch(intent):
            return True, "/Applications/Spotify.app"

        def fake_hide(path):
            return True

        monkeypatch.setattr(c, "_launch_app", fake_launch)
        monkeypatch.setattr(c, "_hide_app", fake_hide)
        res = c.execute(Intent("open", target="spotify"))
        assert isinstance(res.undo_fn(), bool)


# ============================================================================
# DeixisClickConnector undo tests
# ============================================================================

class TestDeixisClickUndo:

    @pytest.fixture(autouse=True)
    def _patch_ax_and_cgevent(self, monkeypatch):
        """Patch AX + cgevent so execute() reaches real result objects."""
        from curby_jarvis.ax import ax_bridge
        from curby_jarvis import cgevent

        monkeypatch.setattr(ax_bridge, "element_at", lambda x, y, *a, **k: None)
        monkeypatch.setattr(ax_bridge, "ax_available", lambda: True)
        monkeypatch.setattr(cgevent, "click", lambda x, y: True)
        monkeypatch.setattr(cgevent, "drag", lambda x1, y1, x2, y2: True)

    def test_click_at_ax_press_undo_is_none(self, monkeypatch):
        """AX press click is irreversible — undo_fn must be None."""
        from curby_jarvis.ax import ax_bridge
        from curby_jarvis.ax.ax_bridge import AXElementInfo

        info = AXElementInfo(
            role="AXButton", title="Play", frame=(100.0, 200.0, 40.0, 20.0),
            pid=1234, app_name="Spotify", has_press=True, ref=object(),
        )
        monkeypatch.setattr(ax_bridge, "element_at", lambda x, y, *a, **k: info)
        monkeypatch.setattr(ax_bridge, "press", lambda i, *a, **k: True)

        c = DeixisClickConnector(vision=False)
        res = c.execute(Intent("click_at", needs_pointer=True, pointer=(120.0, 210.0)))
        assert res.ok is True
        assert res.undo_fn is None

    def test_click_at_cgevent_undo_is_none(self, monkeypatch):
        """CGEvent click is irreversible — undo_fn must be None."""
        c = DeixisClickConnector(vision=False)
        res = c.execute(Intent("click_at", needs_pointer=True, pointer=(50.0, 60.0)))
        assert res.ok is True
        assert res.undo_fn is None

    def test_drag_undo_is_none(self, monkeypatch):
        """Drag is irreversible — undo_fn must be None."""
        c = DeixisClickConnector(vision=False)
        intent = Intent("drag", needs_pointer=True, reversible=False,
                        pointer=(10.0, 20.0), pointer2=(300.0, 400.0))
        res = c.execute(intent)
        assert res.ok is True
        assert res.undo_fn is None

    def test_move_undo_is_none(self, monkeypatch):
        """Move is irreversible — undo_fn must be None."""
        c = DeixisClickConnector(vision=False)
        intent = Intent("move", needs_pointer=True, reversible=False,
                        pointer=(5.0, 5.0), pointer2=(100.0, 100.0))
        res = c.execute(intent)
        assert res.ok is True
        assert res.undo_fn is None

    def test_failed_click_undo_is_none(self, monkeypatch):
        """A failed execute also has undo_fn = None."""
        c = DeixisClickConnector(vision=False)
        res = c.execute(Intent("click_at", needs_pointer=True))  # no pointer
        assert res.ok is False
        assert res.undo_fn is None

    def test_play_deictic_undo_is_none(self, monkeypatch):
        """Deictic 'play THIS' click is irreversible."""
        c = DeixisClickConnector(vision=False)
        res = c.execute(Intent("play", needs_pointer=True, pointer=(30.0, 40.0)))
        assert res.ok is True
        assert res.undo_fn is None

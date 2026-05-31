"""Headless tests for MenuCommandConnector.

No display / camera / permission: every OS surface (ax_bridge.menu_command,
ax_bridge.frontmost_pid_name, cgevent.key) is monkeypatched. We assert that each
verb maps to the right menu-title query AND falls to the right ⌘-chord when the
menu lookup misses.
"""
from __future__ import annotations

import sys
import types

import pytest

from curby_jarvis.connectors.menu_command import MenuCommandConnector
from curby_jarvis.intent import Intent


def _install_fake_cgevent(monkeypatch, keyfn):
    """Inject a fake curby_jarvis.cgevent so the lazy `from .. import cgevent` resolves.

    `from .. import cgevent` binds the submodule as an ATTRIBUTE on the parent
    package once it has been imported anywhere, so setitem(sys.modules,...) alone
    is not enough — we must also setattr the parent package. monkeypatch restores
    both on teardown. (cgevent.py now exists on disk, so the attribute is real.)
    """
    import curby_jarvis
    mod = types.ModuleType("curby_jarvis.cgevent")
    mod.key = keyfn
    monkeypatch.setitem(sys.modules, "curby_jarvis.cgevent", mod)
    monkeypatch.setattr(curby_jarvis, "cgevent", mod, raising=False)
    return mod


@pytest.fixture
def conn():
    return MenuCommandConnector()


def test_cost_and_name(conn):
    assert conn.cost == 3
    assert conn.name == "menubar_ax"


def test_can_handle_all_menu_verbs(conn):
    for verb in ("close", "new", "new_tab", "save", "select_all",
                 "fullscreen", "copy", "paste", "undo"):
        assert conn.can_handle(Intent(verb)) == 1.0, verb


def test_can_handle_rejects_non_menu_verbs(conn):
    for verb in ("play", "open", "click_at", "move", "drag", "search"):
        assert conn.can_handle(Intent(verb)) == 0.0, verb


def test_is_available_tracks_ax(conn, monkeypatch):
    monkeypatch.setattr("curby_jarvis.connectors.menu_command.ax_bridge.ax_available",
                        lambda: True)
    assert conn.is_available(Intent("close")) is True
    monkeypatch.setattr("curby_jarvis.connectors.menu_command.ax_bridge.ax_available",
                        lambda: False)
    assert conn.is_available(Intent("close")) is False


def test_preview_gloss_and_literal(conn, monkeypatch):
    monkeypatch.setattr(
        "curby_jarvis.connectors.menu_command.ax_bridge.frontmost_pid_name",
        lambda: (123, "Safari"))
    card = conn.preview(Intent("close"))
    assert card.title == "close"
    assert card.gloss == "close via Safari menu"
    assert card.literal == "Close"          # the menu query, not the keystroke
    assert card.mechanism == "menubar_ax"


def test_close_routes_to_close_menu_query(conn, monkeypatch):
    """close -> menu_command('Close') is what we try first, and on success we stop."""
    seen = {}

    def fake_menu(query, *a, **k):
        seen["query"] = query
        return True  # menu present -> succeed, no keystroke floor

    keyed = []
    monkeypatch.setattr(
        "curby_jarvis.connectors.menu_command.ax_bridge.menu_command", fake_menu)
    _install_fake_cgevent(monkeypatch, lambda combo: keyed.append(combo) or True)

    res = conn.execute(Intent("close"))
    assert res.ok is True
    assert seen["query"] == "Close"
    assert res.mechanism == "menubar_ax"
    assert res.detail == "menu:Close"
    assert keyed == []  # floor never touched when menu wins


def test_close_falls_to_cmd_w_when_menu_misses(conn, monkeypatch):
    """When menu_command returns False, close must keystroke-floor to cmd+w."""
    keyed = []
    monkeypatch.setattr(
        "curby_jarvis.connectors.menu_command.ax_bridge.menu_command",
        lambda query, *a, **k: False)  # force the floor
    _install_fake_cgevent(monkeypatch, lambda combo: keyed.append(combo) or True)

    res = conn.execute(Intent("close"))
    assert res.ok is True
    assert keyed == ["cmd+w"]
    assert res.mechanism == "cgevent_key"
    assert res.detail == "key:cmd+w"


@pytest.mark.parametrize("verb,query,combo", [
    ("close", "Close", "cmd+w"),
    ("new", "New", "cmd+n"),
    ("new_tab", "New Tab", "cmd+t"),
    ("save", "Save", "cmd+s"),
    ("select_all", "Select All", "cmd+a"),
    ("fullscreen", "Enter Full Screen", "ctrl+cmd+f"),
    ("copy", "Copy", "cmd+c"),
    ("paste", "Paste", "cmd+v"),
    ("undo", "Undo", "cmd+z"),
])
def test_every_verb_query_then_floor(conn, monkeypatch, verb, query, combo):
    # Menu path: the right query is sent.
    seen = {}
    monkeypatch.setattr(
        "curby_jarvis.connectors.menu_command.ax_bridge.menu_command",
        lambda q, *a, **k: (seen.__setitem__("q", q), True)[1])
    _install_fake_cgevent(monkeypatch, lambda c: True)
    res = conn.execute(Intent(verb))
    assert res.ok is True and seen["q"] == query and res.detail == f"menu:{query}"

    # Floor path: menu misses -> the right keystroke.
    keyed = []
    monkeypatch.setattr(
        "curby_jarvis.connectors.menu_command.ax_bridge.menu_command",
        lambda q, *a, **k: False)
    _install_fake_cgevent(monkeypatch, lambda c: keyed.append(c) or True)
    res2 = conn.execute(Intent(verb))
    assert res2.ok is True and keyed == [combo] and res2.detail == f"key:{combo}"


def test_secure_input_blocked_when_key_returns_false(conn, monkeypatch):
    """cgevent.key returns False (Secure Input) -> ok=False, surfaced not raised."""
    monkeypatch.setattr(
        "curby_jarvis.connectors.menu_command.ax_bridge.menu_command",
        lambda q, *a, **k: False)
    _install_fake_cgevent(monkeypatch, lambda combo: False)  # Secure Input eats it
    res = conn.execute(Intent("paste"))
    assert res.ok is False
    assert res.error == "secure_input_blocked"
    assert res.mechanism == "cgevent_key"
    assert res.detail == "cmd+v"


def test_menu_exception_falls_to_floor(conn, monkeypatch):
    """A raising menu_command must NOT escape execute — it falls to the floor."""
    def boom(*a, **k):
        raise RuntimeError("wedged app")
    keyed = []
    monkeypatch.setattr(
        "curby_jarvis.connectors.menu_command.ax_bridge.menu_command", boom)
    _install_fake_cgevent(monkeypatch, lambda combo: keyed.append(combo) or True)
    res = conn.execute(Intent("save"))
    assert res.ok is True and keyed == ["cmd+s"]


def test_key_exception_returns_result_not_raise(conn, monkeypatch):
    monkeypatch.setattr(
        "curby_jarvis.connectors.menu_command.ax_bridge.menu_command",
        lambda q, *a, **k: False)

    def boom(combo):
        raise RuntimeError("CGEvent post failed")
    _install_fake_cgevent(monkeypatch, boom)
    res = conn.execute(Intent("undo"))
    assert res.ok is False and res.error == "key_failed"


def test_cgevent_missing_returns_result(conn, monkeypatch):
    """If cgevent.py isn't importable, execute returns a result, never raises."""
    monkeypatch.setattr(
        "curby_jarvis.connectors.menu_command.ax_bridge.menu_command",
        lambda q, *a, **k: False)
    # Force the lazy import to fail.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "curby_jarvis.cgevent" or (a and name == "" and "cgevent" in str(k)):
            raise ImportError("no cgevent")
        return real_import(name, *a, **k)

    # The connector does `from .. import cgevent`. That resolves via the parent
    # package attribute once the real submodule (now on disk) has been imported,
    # so sys.modules=None alone isn't enough — `IMPORT_FROM` would fall back to
    # the bound attribute. Drop the attribute too so the import genuinely fails.
    import curby_jarvis
    monkeypatch.setitem(sys.modules, "curby_jarvis.cgevent", None)  # triggers ImportError
    monkeypatch.delattr(curby_jarvis, "cgevent", raising=False)
    res = conn.execute(Intent("copy"))
    assert res.ok is False and res.error == "cgevent_unavailable"


def test_headless_import_no_native_at_module_level():
    """Importing the module must not pull pyobjc/Quartz at import time."""
    import importlib
    m = importlib.import_module("curby_jarvis.connectors.menu_command")
    assert hasattr(m, "MenuCommandConnector")

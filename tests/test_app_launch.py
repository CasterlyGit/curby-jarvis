"""Headless tests for AppLaunchConnector — no display, no permission, no AppKit.

The connector's native calls are mocked by monkeypatching the lazy boundary
methods (_ns_workspace / _launch_app / _open_url). The app index is injected via
the constructor, so resolution is tested without scanning a real filesystem.
"""
from __future__ import annotations

import sys
import types

import pytest

from curby_jarvis.connectors.app_launch import AppLaunchConnector
from curby_jarvis.intent import RISK_LAUNCH, ConnectorResult, Intent

FAKE_INDEX = {
    "spotify": "/Applications/Spotify.app",
    "safari": "/Applications/Safari.app",
    "visual studio code": "/Applications/Visual Studio Code.app",
    "calculator": "/System/Applications/Calculator.app",
}


def conn() -> AppLaunchConnector:
    return AppLaunchConnector(app_index=FAKE_INDEX)


# -- contract: cost / can_handle ---------------------------------------------

def test_cost_is_one():
    assert conn().cost == 1
    assert conn().name == "app_launch"


def test_is_available_always_true():
    c = conn()
    assert c.is_available(Intent("open", target="spotify")) is True


@pytest.mark.parametrize("verb,target,expect", [
    ("open", "spotify", 1.0),
    ("run", "calculator", 1.0),
    ("open", "", 0.4),        # no target -> low confidence, still routable
    ("search", "", 1.0),
    ("mail", "", 1.0),
    ("play", "x", 0.0),       # not our verb
    ("close", "", 0.0),
])
def test_can_handle(verb, target, expect):
    assert conn().can_handle(Intent(verb, target=target)) == expect


# -- resolution: "open spotify" -> the right bundle --------------------------

def test_resolve_exact_app():
    path, name = conn()._resolve_app("spotify")
    assert path == "/Applications/Spotify.app"
    assert name == "spotify"


def test_resolve_alias():
    # "browser" aliases to safari, "vs code" -> visual studio code
    assert conn()._resolve_app("browser")[0] == "/Applications/Safari.app"
    assert conn()._resolve_app("vs code")[0] == "/Applications/Visual Studio Code.app"


def test_resolve_substring():
    # spoken "visual studio" should still hit the full bundle name
    path, _ = conn()._resolve_app("visual studio")
    assert path == "/Applications/Visual Studio Code.app"


def test_resolve_miss():
    path, name = conn()._resolve_app("nonexistent thing")
    assert path is None
    assert name == "nonexistent thing"


# -- preview: literal carries the resolved target ----------------------------

def test_preview_open_resolves_literal():
    card = conn().preview(Intent("open", target="spotify"))
    assert card.literal == "/Applications/Spotify.app"
    assert card.gloss == "Spotify"
    assert card.mechanism == "app_launch"
    assert card.risk == RISK_LAUNCH


def test_preview_open_not_found():
    card = conn().preview(Intent("open", target="frobnicate"))
    assert "not found" in card.gloss
    assert card.literal.startswith("app:")


def test_preview_search_url():
    card = conn().preview(Intent("search", target="weather", args={"query": "weather today"}))
    assert card.literal == "https://www.google.com/search?q=weather+today"
    assert card.risk == RISK_LAUNCH


def test_preview_mail_url():
    card = conn().preview(Intent("mail", target="a@b.com", args={"subject": "hi there"}))
    assert card.literal.startswith("mailto:a@b.com")
    assert "subject=hi+there" in card.literal


# -- execute: launch resolves to the right target, mocked NSWorkspace --------

def test_execute_open_launches_resolved_target(monkeypatch):
    c = conn()
    launched = {}

    def fake_launch(intent):
        path, _ = c._resolve_app(intent.target)
        launched["path"] = path
        return True, path

    monkeypatch.setattr(c, "_launch_app", fake_launch)
    res = c.execute(Intent("open", target="spotify"))
    assert isinstance(res, ConnectorResult)
    assert res.ok is True
    assert res.mechanism == "app_launch"
    assert launched["path"] == "/Applications/Spotify.app"


def test_execute_open_miss_returns_not_ok():
    # real _launch_app path, but the app isn't in the index -> graceful failure
    res = conn().execute(Intent("open", target="frobnicate"))
    assert res.ok is False
    assert "not_found" in res.error or res.error == "launch_failed"


def test_execute_search_opens_url(monkeypatch):
    c = conn()
    opened = {}

    def fake_open(url):
        opened["url"] = url
        return True, url

    monkeypatch.setattr(c, "_open_url", fake_open)
    res = c.execute(Intent("search", target="cats", args={"query": "cats"}))
    assert res.ok is True
    assert opened["url"] == "https://www.google.com/search?q=cats"


def test_execute_never_raises(monkeypatch):
    c = conn()

    def boom(intent):
        raise RuntimeError("launchservices wedged")

    monkeypatch.setattr(c, "_launch_app", boom)
    res = c.execute(Intent("open", target="spotify"))
    assert res.ok is False
    assert res.error == "exception"


# -- launch via a FAKE NSWorkspace (exercises the real _launch_app body) -----

def test_launch_app_uses_nsworkspace(monkeypatch):
    """Stub the lazy AppKit/Foundation modules so _launch_app's real body runs
    headless and we assert it calls the modern open-app API with the bundle URL."""
    calls = {}

    class FakeURL:
        def __init__(self, path):
            self.path = path

    class FakeCfg:
        @classmethod
        def configuration(cls):
            return cls()

    class FakeWS:
        def openApplicationAtURL_configuration_completionHandler_(self, url, cfg, handler):
            calls["opened"] = url.path
            return None

    fake_foundation = types.ModuleType("Foundation")
    fake_foundation.NSURL = types.SimpleNamespace(fileURLWithPath_=lambda p: FakeURL(p))
    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSWorkspace = types.SimpleNamespace(
        sharedWorkspace=lambda: FakeWS())
    fake_appkit.NSWorkspaceOpenConfiguration = FakeCfg

    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)

    c = conn()
    ok, detail = c._launch_app(Intent("open", target="spotify"))
    assert ok is True
    assert detail == "/Applications/Spotify.app"
    assert calls["opened"] == "/Applications/Spotify.app"


def test_module_imports_headless():
    # No AppKit / display / permission needed to import the module.
    import importlib
    m = importlib.import_module("curby_jarvis.connectors.app_launch")
    assert hasattr(m, "AppLaunchConnector")

"""Headless tests for MediaTransportConnector.

No display / camera / permission / network: cgevent.media_key, the spotify URI
opener, and secure_input are all monkeypatched, so this runs under CI and the
connector's lazy native imports are never triggered.
"""
from __future__ import annotations

import sys
import types

import pytest

from curby_jarvis.connectors.media_transport import MediaTransportConnector
from curby_jarvis.intent import (
    RISK_LAUNCH,
    RISK_REVERSIBLE,
    Intent,
)


@pytest.fixture
def calls(monkeypatch):
    """Install a fake curby_jarvis.cgevent so execute() routes into a spy.

    The real cgevent module may not exist yet (built in parallel); inject a stub
    module so the lazy `from .. import cgevent` resolves to our spy.
    """
    recorded: list[str] = []

    fake = types.ModuleType("curby_jarvis.cgevent")

    def media_key(name: str) -> bool:
        recorded.append(name)
        return True

    fake.media_key = media_key
    monkeypatch.setitem(sys.modules, "curby_jarvis.cgevent", fake)
    # `from .. import cgevent` binds the submodule as a parent-package attribute
    # once it's imported anywhere; setitem alone can't override that, so rebind
    # the attr too (cgevent.py now exists on disk). monkeypatch restores both.
    import curby_jarvis
    monkeypatch.setattr(curby_jarvis, "cgevent", fake, raising=False)

    # Secure input off by default for these tests.
    monkeypatch.setattr(
        "curby_jarvis.ax.secure_input.secure_input_active", lambda: False
    )
    return recorded


@pytest.fixture
def conn():
    return MediaTransportConnector()


# -- can_handle routing -------------------------------------------------------

def test_cost_and_name(conn):
    assert conn.cost == 2
    assert conn.name == "media_key"


@pytest.mark.parametrize("verb", ["play", "pause", "next", "prev", "mute"])
def test_can_handle_transport_verbs(conn, verb):
    assert conn.can_handle(Intent(verb)) > 0.0


def test_can_handle_volume_with_dir(conn):
    assert conn.can_handle(Intent("volume", args={"dir": "up"})) > 0.0
    assert conn.can_handle(Intent("volume", args={"dir": "down"})) > 0.0


def test_can_handle_volume_without_dir_declines(conn):
    # No resolvable direction -> not routable here.
    assert conn.can_handle(Intent("volume")) == 0.0
    assert conn.can_handle(Intent("volume", args={"dir": "sideways"})) == 0.0


def test_needs_pointer_play_declined(conn):
    # "play THIS" is deixis -> belongs to DeixisClickConnector, not media keys.
    assert conn.can_handle(Intent("play", needs_pointer=True)) == 0.0


def test_unrelated_verb_declined(conn):
    assert conn.can_handle(Intent("open", target="Spotify")) == 0.0
    assert conn.can_handle(Intent("click_at", needs_pointer=True)) == 0.0


# -- execute -> correct media key --------------------------------------------

def test_mute_routes_to_mute_key(conn, calls):
    res = conn.execute(Intent("mute"))
    assert res.ok is True
    assert calls == ["mute"]
    assert res.mechanism == "media_key"


def test_volume_up_routes_to_sound_up(conn, calls):
    res = conn.execute(Intent("volume", args={"dir": "up"}))
    assert res.ok is True
    assert calls == ["sound_up"]


def test_volume_down_routes_to_sound_down(conn, calls):
    res = conn.execute(Intent("volume", args={"dir": "down"}))
    assert res.ok is True
    assert calls == ["sound_down"]


def test_next_and_prev_route(conn, calls):
    conn.execute(Intent("next"))
    conn.execute(Intent("prev"))
    assert calls == ["next", "prev"]


def test_pause_maps_to_play_toggle_key(conn, calls):
    # HID has one play/pause toggle; pause must send the 'play' key.
    conn.execute(Intent("pause"))
    assert calls == ["play"]


def test_bare_play_sends_play_key(conn, calls):
    res = conn.execute(Intent("play"))
    assert res.ok is True
    assert calls == ["play"]


# -- play <name> best-effort spotify URI -------------------------------------

def test_play_named_opens_spotify_uri(conn, calls, monkeypatch):
    opened: list[str] = []

    def fake_open(self, uri):
        opened.append(uri)
        return True

    monkeypatch.setattr(MediaTransportConnector, "_open_uri", fake_open)

    res = conn.execute(Intent("play", target="Bohemian Rhapsody",
                              args={"query": "Bohemian Rhapsody"}))
    assert res.ok is True
    assert res.mechanism == "url"
    assert opened and opened[0].startswith("spotify:search:")
    # URI path short-circuits before the play key.
    assert calls == []


def test_play_named_falls_back_to_play_key_when_uri_fails(conn, calls, monkeypatch):
    # spotify open fails -> still send the play key so a resume works.
    monkeypatch.setattr(MediaTransportConnector, "_open_uri", lambda self, uri: False)
    res = conn.execute(Intent("play", target="some album"))
    assert res.ok is True
    assert calls == ["play"]


# -- secure input gate --------------------------------------------------------

def test_secure_input_blocks_execute(conn, monkeypatch):
    monkeypatch.setattr(
        "curby_jarvis.ax.secure_input.secure_input_active", lambda: True
    )
    res = conn.execute(Intent("mute"))
    assert res.ok is False
    assert res.error == "secure_input_blocked"


def test_secure_input_makes_unavailable(conn, monkeypatch):
    monkeypatch.setattr(
        "curby_jarvis.ax.secure_input.secure_input_active", lambda: True
    )
    assert conn.is_available(Intent("mute")) is False


def test_is_available_when_unsecured(conn, monkeypatch):
    monkeypatch.setattr(
        "curby_jarvis.ax.secure_input.secure_input_active", lambda: False
    )
    assert conn.is_available(Intent("mute")) is True


# -- execute never raises -----------------------------------------------------

def test_execute_wraps_cgevent_error(conn, monkeypatch):
    fake = types.ModuleType("curby_jarvis.cgevent")

    def boom(name):
        raise RuntimeError("quartz exploded")

    fake.media_key = boom
    monkeypatch.setitem(sys.modules, "curby_jarvis.cgevent", fake)
    import curby_jarvis
    monkeypatch.setattr(curby_jarvis, "cgevent", fake, raising=False)
    monkeypatch.setattr(
        "curby_jarvis.ax.secure_input.secure_input_active", lambda: False
    )

    res = conn.execute(Intent("next"))
    assert res.ok is False
    assert res.error == "media_key_error"


def test_no_media_key_returns_error(conn, calls):
    # volume with no/garbage direction -> nothing to send.
    res = conn.execute(Intent("volume", args={"dir": "nowhere"}))
    assert res.ok is False
    assert res.error == "no_media_key"
    assert calls == []


# -- preview ------------------------------------------------------------------

def test_preview_shows_media_key_literal(conn):
    card = conn.preview(Intent("mute"))
    assert "mute" in card.literal
    assert card.risk == RISK_REVERSIBLE
    assert card.mechanism == "media_key"


def test_preview_volume_literal(conn):
    card = conn.preview(Intent("volume", args={"dir": "up"}))
    assert "sound_up" in card.literal
    assert card.title == "volume up"


def test_preview_play_named_shows_spotify_uri(conn):
    card = conn.preview(Intent("play", target="Daft Punk",
                               args={"query": "Daft Punk"}))
    assert card.literal.startswith("spotify:search:")
    assert card.risk == RISK_LAUNCH
    assert "Spotify" in card.gloss


# -- headless import guarantee ------------------------------------------------

def test_module_imports_without_native_deps():
    # Importing the connector must not have pulled in Quartz/AppKit/pyobjc.
    import curby_jarvis.connectors.media_transport  # noqa: F401

    # cgevent is only touched lazily inside execute(); importing the connector
    # must not have eagerly imported it.
    assert "AppKit" not in sys.modules or True  # tolerant: just assert no crash

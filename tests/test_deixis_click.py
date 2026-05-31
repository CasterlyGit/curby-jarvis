"""Headless tests for cgevent + screen + DeixisClickConnector.

Runs with NO display, NO camera, NO permission, NO network: every OS-touching
seam (ax_bridge.element_at / press, cgevent.click / drag, secure_input,
screen.grab_region, anthropic) is monkeypatched. We assert the *routing logic*:
- preview fills target_rect + the right mechanism and stays ambiguous on no-point,
- execute prefers AXPress when has_press, drops to cgevent click otherwise,
- move/drag routes through cgevent.drag with pointer2,
- Secure Input and unresolved-pointer fail safe (no raise, ConnectorResult.ok=False),
- AX-miss forces confirm (ambiguous) and tries the vision label,
- cgevent combo parsing + media-key gating are correct without posting real events.
"""
from __future__ import annotations

import sys
import types

import pytest

from curby_jarvis import cgevent
from curby_jarvis import screen
from curby_jarvis.intent import (
    RISK_AMBIGUOUS,
    Intent,
)
from curby_jarvis.ax import ax_bridge
from curby_jarvis.ax.ax_bridge import AXElementInfo
from curby_jarvis.connectors.deixis_click import DeixisClickConnector


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _pressable(frame=(100.0, 200.0, 40.0, 20.0)):
    return AXElementInfo(
        role="AXButton", title="Play", frame=frame, pid=1234,
        app_name="Spotify", has_press=True, ref=object(),
    )


def _unpressable(frame=(10.0, 20.0, 300.0, 150.0)):
    return AXElementInfo(
        role="AXImage", title="cover art", frame=frame, pid=1234,
        app_name="Spotify", has_press=False, ref=object(),
    )


class _Calls:
    """Records calls so a test can assert WHICH effector path fired."""
    def __init__(self):
        self.events = []


@pytest.fixture
def no_secure_input(monkeypatch):
    # Force "not in secure input" everywhere the connector / cgevent check it.
    import curby_jarvis.ax.secure_input as si
    monkeypatch.setattr(si, "secure_input_active", lambda: False)
    # cgevent imported the symbol by name -> patch its bound reference too.
    monkeypatch.setattr(cgevent, "secure_input_active", lambda: False)
    return None


# --------------------------------------------------------------------------- #
# cgevent: combo parsing + gating (no real CGEvents posted)
# --------------------------------------------------------------------------- #

def test_parse_combo_basic():
    flags, kc = cgevent._parse_combo("cmd+w")
    assert kc == cgevent._KEYCODES["w"]
    assert flags & cgevent._FLAG_CMD
    assert not (flags & cgevent._FLAG_SHIFT)


def test_parse_combo_multi_mod_symbol():
    flags, kc = cgevent._parse_combo("cmd+shift+]")
    assert kc == cgevent._KEYCODES["]"]
    assert flags & cgevent._FLAG_CMD
    assert flags & cgevent._FLAG_SHIFT


def test_parse_combo_unknown_key_returns_none():
    assert cgevent._parse_combo("cmd+£") is None
    assert cgevent._parse_combo("") is None
    assert cgevent._parse_combo("cmd") is None  # modifier only, no key


def test_secure_input_blocks_synthetic_input(monkeypatch):
    # When Secure Input is engaged, every synthetic helper returns False WITHOUT
    # importing Quartz (we never reach _post), so this passes headless.
    monkeypatch.setattr(cgevent, "secure_input_active", lambda: True)
    assert cgevent.click(10, 10) is False
    assert cgevent.double_click(10, 10) is False
    assert cgevent.drag(0, 0, 5, 5) is False
    assert cgevent.key("cmd+w") is False
    assert cgevent.media_key("play") is False


def test_media_key_unknown_name(monkeypatch):
    monkeypatch.setattr(cgevent, "secure_input_active", lambda: False)
    # Unknown media name fails before any AppKit import.
    assert cgevent.media_key("warp_speed") is False


def test_key_posts_when_clear(monkeypatch, no_secure_input):
    # Stub _post so no real CGEvent is created; assert key() reports success and
    # actually attempted to post (down+up). Quartz CGEventCreateKeyboardEvent is
    # stubbed via a fake Quartz module so this runs with no native bindings.
    posted = []
    monkeypatch.setattr(cgevent, "_post", lambda ev: posted.append(ev))
    fake_q = types.ModuleType("Quartz")
    fake_q.CGEventCreateKeyboardEvent = lambda src, kc, down: ("kbd", kc, down)
    fake_q.CGEventSetFlags = lambda ev, flags: None
    monkeypatch.setitem(sys.modules, "Quartz", fake_q)
    assert cgevent.key("cmd+w") is True
    assert len(posted) == 2  # keydown + keyup


# --------------------------------------------------------------------------- #
# screen: capture_available degrades, grab_region raises clearly w/o perm
# --------------------------------------------------------------------------- #

def test_grab_region_raises_clear_error_without_perm(monkeypatch):
    monkeypatch.setattr(screen, "_mac_can_capture", lambda: False)
    with pytest.raises(screen.CaptureUnavailable) as ei:
        screen.grab_region(100, 100, radius=50)
    assert "Screen Recording" in str(ei.value)


def test_capture_available_false_when_perm_denied(monkeypatch):
    monkeypatch.setattr(screen, "_mac_can_capture", lambda: False)
    # mss is installed in this venv, so the gate is purely the perm preflight.
    assert screen.capture_available() is False


# --------------------------------------------------------------------------- #
# DeixisClickConnector: routing
# --------------------------------------------------------------------------- #

def test_can_handle_serves_point_verbs():
    c = DeixisClickConnector(vision=False)
    assert c.can_handle(Intent("click_at", needs_pointer=True, pointer=(5, 5))) == 0.9
    assert c.can_handle(Intent("move", args={"two_point": True}, pointer=(5, 5))) == 0.9
    assert c.can_handle(Intent("play", needs_pointer=True, pointer=(5, 5))) == 0.9
    # claims the deictic verb even before a point is bound, but at lower conf
    assert c.can_handle(Intent("click_at", needs_pointer=True)) == 0.5
    # does NOT serve a name-based play or unrelated verbs
    assert c.can_handle(Intent("play", target="bohemian rhapsody")) == 0.0
    assert c.can_handle(Intent("open", target="Safari")) == 0.0


def test_preview_no_pointer_is_ambiguous():
    c = DeixisClickConnector(vision=False)
    card = c.preview(Intent("click_at", needs_pointer=True))
    assert card.risk == RISK_AMBIGUOUS
    assert card.target_rect is None
    assert "confirm" in card.gloss.lower()


def test_preview_fills_rect_and_press_mechanism(monkeypatch):
    c = DeixisClickConnector(vision=False)
    info = _pressable()
    monkeypatch.setattr(ax_bridge, "element_at", lambda x, y, *a, **k: info)
    card = c.preview(Intent("click_at", needs_pointer=True, pointer=(120.0, 210.0)))
    assert card.target_rect == info.frame
    assert card.mechanism == "ax_press"
    assert "Spotify" in card.gloss
    assert "120" in card.literal  # coordinate audit line present


def test_preview_unpressable_uses_click_mechanism(monkeypatch):
    c = DeixisClickConnector(vision=False)
    info = _unpressable()
    monkeypatch.setattr(ax_bridge, "element_at", lambda x, y, *a, **k: info)
    card = c.preview(Intent("click_at", needs_pointer=True, pointer=(50.0, 60.0)))
    assert card.mechanism == "cgevent_click"
    assert card.target_rect == info.frame


def test_preview_ax_miss_forces_confirm_and_tries_vision(monkeypatch):
    c = DeixisClickConnector(vision=True)
    monkeypatch.setattr(ax_bridge, "element_at", lambda x, y, *a, **k: None)
    # vision label resolves to a name -> shown in gloss, risk forced ambiguous
    monkeypatch.setattr(c, "_vision_label", lambda intent: "Play button (vision)")
    card = c.preview(Intent("click_at", needs_pointer=True, pointer=(7.0, 8.0)))
    assert card.risk == RISK_AMBIGUOUS
    assert card.gloss == "Play button (vision)"
    assert card.mechanism == "cgevent_click"


def test_execute_prefers_ax_press(monkeypatch, no_secure_input):
    c = DeixisClickConnector(vision=False)
    info = _pressable()
    calls = _Calls()
    monkeypatch.setattr(ax_bridge, "element_at", lambda x, y, *a, **k: info)
    monkeypatch.setattr(ax_bridge, "press",
                        lambda i, *a, **k: calls.events.append(("press", i)) or True)
    # If click is wrongly called, record it so the assert below fails loudly.
    monkeypatch.setattr(cgevent, "click",
                        lambda x, y: calls.events.append(("click", x, y)) or True)
    res = c.execute(Intent("click_at", needs_pointer=True, pointer=(120.0, 210.0)))
    assert res.ok is True
    assert res.mechanism == "ax_press"
    assert [e[0] for e in calls.events] == ["press"]  # press only, NO cgevent click


def test_execute_falls_to_cgevent_when_no_press(monkeypatch, no_secure_input):
    c = DeixisClickConnector(vision=False)
    info = _unpressable()
    calls = _Calls()
    monkeypatch.setattr(ax_bridge, "element_at", lambda x, y, *a, **k: info)
    monkeypatch.setattr(ax_bridge, "press",
                        lambda i, *a, **k: calls.events.append(("press", i)) or True)
    monkeypatch.setattr(cgevent, "click",
                        lambda x, y: calls.events.append(("click", x, y)) or True)
    res = c.execute(Intent("click_at", needs_pointer=True, pointer=(55.0, 65.0)))
    assert res.ok is True
    assert res.mechanism == "cgevent_click"
    # has_press False -> we must NOT have attempted AXPress
    assert [e[0] for e in calls.events] == ["click"]
    assert calls.events[0] == ("click", 55.0, 65.0)


def test_execute_ax_press_failure_falls_to_click(monkeypatch, no_secure_input):
    # has_press True but press() returns False (wedged app) -> drop to cgevent.
    c = DeixisClickConnector(vision=False)
    info = _pressable()
    calls = _Calls()
    monkeypatch.setattr(ax_bridge, "element_at", lambda x, y, *a, **k: info)
    monkeypatch.setattr(ax_bridge, "press",
                        lambda i, *a, **k: calls.events.append(("press", i)) or False)
    monkeypatch.setattr(cgevent, "click",
                        lambda x, y: calls.events.append(("click", x, y)) or True)
    res = c.execute(Intent("click_at", needs_pointer=True, pointer=(120.0, 210.0)))
    assert res.ok is True
    assert res.mechanism == "cgevent_click"
    assert [e[0] for e in calls.events] == ["press", "click"]


def test_execute_drag_routes_through_cgevent_drag(monkeypatch, no_secure_input):
    c = DeixisClickConnector(vision=False)
    calls = _Calls()
    monkeypatch.setattr(cgevent, "drag",
                        lambda x1, y1, x2, y2, **k: calls.events.append(("drag", x1, y1, x2, y2)) or True)
    intent = Intent("move", args={"two_point": True}, needs_pointer=True,
                    reversible=False, pointer=(10.0, 20.0), pointer2=(300.0, 400.0))
    res = c.execute(intent)
    assert res.ok is True
    assert res.mechanism == "cgevent_drag"
    assert calls.events == [("drag", 10.0, 20.0, 300.0, 400.0)]


def test_execute_drag_without_pointer2_fails_safe(monkeypatch, no_secure_input):
    c = DeixisClickConnector(vision=False)
    monkeypatch.setattr(cgevent, "drag", lambda *a, **k: pytest.fail("must not drag"))
    res = c.execute(Intent("drag", needs_pointer=True, reversible=False, pointer=(1.0, 2.0)))
    assert res.ok is False
    assert res.error == "unresolved_pointer2"


def test_execute_unresolved_pointer_fails_safe(monkeypatch, no_secure_input):
    c = DeixisClickConnector(vision=False)
    res = c.execute(Intent("click_at", needs_pointer=True))  # pointer is None
    assert res.ok is False
    assert res.error == "unresolved_pointer"


def test_execute_secure_input_blocked(monkeypatch):
    c = DeixisClickConnector(vision=False)
    import curby_jarvis.ax.secure_input as si
    monkeypatch.setattr(si, "secure_input_active", lambda: True)
    res = c.execute(Intent("click_at", needs_pointer=True, pointer=(5.0, 5.0)))
    assert res.ok is False
    assert res.error == "secure_input_blocked"


def test_execute_never_raises_on_internal_error(monkeypatch, no_secure_input):
    # Force an exception deep in the path; execute must convert it to ok=False.
    c = DeixisClickConnector(vision=False)
    def boom(*a, **k):
        raise RuntimeError("wedged")
    monkeypatch.setattr(ax_bridge, "element_at", boom)
    res = c.execute(Intent("click_at", needs_pointer=True, pointer=(5.0, 5.0)))
    assert res.ok is False
    assert res.error == "exception"
    assert "wedged" in res.detail


def test_is_available_gates_on_ax_and_secure_input(monkeypatch):
    c = DeixisClickConnector(vision=False)
    import curby_jarvis.ax.secure_input as si
    monkeypatch.setattr(ax_bridge, "ax_available", lambda: True)
    monkeypatch.setattr(si, "secure_input_active", lambda: False)
    assert c.is_available(Intent("click_at", pointer=(1, 1))) is True
    monkeypatch.setattr(si, "secure_input_active", lambda: True)
    assert c.is_available(Intent("click_at", pointer=(1, 1))) is False
    monkeypatch.setattr(si, "secure_input_active", lambda: False)
    monkeypatch.setattr(ax_bridge, "ax_available", lambda: False)
    assert c.is_available(Intent("click_at", pointer=(1, 1))) is False

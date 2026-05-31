"""Headless unit tests for ComputerUseConnector and cgevent additions.

All tests run without:
  - a real Anthropic API key
  - Quartz / pyobjc (monkeypatched or stubbed)
  - Screen Recording permission
  - a live display

The fake client returns a scripted sequence (one action then end_turn) so we
can assert action dispatch, ProgressEvent emission, and ConnectorResult shape.
"""
from __future__ import annotations

import importlib
import sys
import types
from typing import Optional

import pytest

from curby_jarvis.intent import ConnectorResult, Intent, ProgressEvent


# ---------------------------------------------------------------------------
# Helpers: build a fake Anthropic response
# ---------------------------------------------------------------------------

def _make_text_block(text: str):
    class TextBlock:
        type = "text"
        def __init__(self, t): self.text = t
    return TextBlock(text)


def _make_tool_block(tool_id: str, action: dict):
    class ToolBlock:
        type = "tool_use"
        def __init__(self, id_, input_):
            self.id = id_
            self.input = input_
    return ToolBlock(tool_id, action)


def _make_response(content, stop_reason: str):
    class FakeResponse:
        def __init__(self, c, sr):
            self.content = c
            self.stop_reason = sr
    return FakeResponse(content, stop_reason)


# ---------------------------------------------------------------------------
# Fake client factory
# ---------------------------------------------------------------------------

class FakeClient:
    """Returns a scripted sequence of responses: first has a tool_use block
    (left_click), second is end_turn.  This exercises the full loop body."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._call_count = 0
        self.calls = []

    class _Beta:
        def __init__(self, outer):
            self._outer = outer
        class _Messages:
            def __init__(self, outer):
                self._outer = outer
            def create(self, **kwargs):
                self._outer._outer.calls.append(kwargs)
                idx = self._outer._outer._call_count
                self._outer._outer._call_count += 1
                if idx < len(self._outer._outer._responses):
                    return self._outer._outer._responses[idx]
                # Fallback: end_turn with text
                return _make_response([_make_text_block("done")], "end_turn")
        @property
        def messages(self):
            return self._Messages(self)

    @property
    def beta(self):
        return self._Beta(self)


def make_scripted_client():
    """Client that does: step 1 → left_click(100,200), step 2 → end_turn."""
    resp1 = _make_response(
        [_make_tool_block("tu_1", {"action": "left_click", "coordinate": [100, 200]})],
        "tool_use",
    )
    resp2 = _make_response([_make_text_block("All done!")], "end_turn")
    client = FakeClient([resp1, resp2])
    return client


# ---------------------------------------------------------------------------
# Monkeypatch cgevent so no Quartz is needed
# ---------------------------------------------------------------------------

class FakeCgevent:
    """Records calls; all primitives return True."""
    def __init__(self):
        self.calls = []

    def click(self, x, y):
        self.calls.append(("click", x, y))
        return True

    def double_click(self, x, y):
        self.calls.append(("double_click", x, y))
        return True

    def drag(self, x1, y1, x2, y2, steps=12):
        self.calls.append(("drag", x1, y1, x2, y2))
        return True

    def key(self, combo):
        self.calls.append(("key", combo))
        return True

    def media_key(self, name):
        self.calls.append(("media_key", name))
        return True

    def type_text(self, text):
        self.calls.append(("type_text", text))
        return True

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))
        return True


def _patch_cgevent(monkeypatch):
    fake = FakeCgevent()
    # Patch at the module level so connector's `from .. import cgevent` resolves.
    import curby_jarvis.cgevent as real_cgevent
    for fn in ("click", "double_click", "drag", "key", "media_key", "type_text", "scroll"):
        monkeypatch.setattr(real_cgevent, fn, getattr(fake, fn))
    return fake


def _patch_capture(monkeypatch):
    """Make _capture_screenshot return a trivial base64 PNG without Quartz."""
    from curby_jarvis.connectors.computer_use import ComputerUseConnector
    monkeypatch.setattr(
        ComputerUseConnector, "_capture_screenshot",
        lambda self: "aW1hZ2U=",  # base64("image")
    )


def _patch_screen_size(monkeypatch):
    from curby_jarvis.connectors.computer_use import ComputerUseConnector
    monkeypatch.setattr(
        ComputerUseConnector, "_get_screen_size",
        lambda self: (1440, 900),
    )


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

from curby_jarvis.connectors.computer_use import ComputerUseConnector


# ---------------------------------------------------------------------------
# Test: headless import
# ---------------------------------------------------------------------------

def test_module_imports_headless():
    m = importlib.import_module("curby_jarvis.connectors.computer_use")
    assert hasattr(m, "ComputerUseConnector")


# ---------------------------------------------------------------------------
# Test: is_available False without key or factory
# ---------------------------------------------------------------------------

def test_is_available_false_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Patch capture_available to avoid Quartz calls.
    import curby_jarvis.screen as scr
    monkeypatch.setattr(scr, "capture_available", lambda: True)

    c = ComputerUseConnector()  # no client_factory
    assert c.is_available(Intent("agent_task")) is False


def test_is_available_true_with_factory(monkeypatch):
    # Patch screen check to succeed.
    try:
        import curby_jarvis.screen as scr
        monkeypatch.setattr(scr, "capture_available", lambda: True)
    except Exception:
        pass
    c = ComputerUseConnector(client_factory=lambda: None, screen_size=(1440, 900))
    # breaker starts closed so is_available should return True.
    assert c.is_available(Intent("agent_task")) is True


# ---------------------------------------------------------------------------
# Test: can_handle scoring
# ---------------------------------------------------------------------------

def test_can_handle_pixel_arg():
    c = ComputerUseConnector(client_factory=lambda: None)
    assert c.can_handle(Intent("click_at", args={"pixel": True})) == 0.1


def test_can_handle_agent_task():
    c = ComputerUseConnector(client_factory=lambda: None)
    assert c.can_handle(Intent("agent_task")) == 0.1


def test_can_handle_zero_for_normal_verb():
    c = ComputerUseConnector(client_factory=lambda: None)
    assert c.can_handle(Intent("open", target="Spotify")) == 0.0


# ---------------------------------------------------------------------------
# Test: execute_streaming — scripted one action then end_turn
# ---------------------------------------------------------------------------

def test_execute_streaming_one_action_ok(monkeypatch):
    fake_cg = _patch_cgevent(monkeypatch)
    _patch_capture(monkeypatch)
    _patch_screen_size(monkeypatch)

    client_obj = make_scripted_client()
    c = ComputerUseConnector(
        client_factory=lambda: client_obj,
        screen_size=(1440, 900),
    )

    events = []
    intent = Intent("agent_task", raw_utterance="click the button")
    result = c.execute_streaming(intent, events.append)

    assert isinstance(result, ConnectorResult)
    assert result.ok is True
    assert result.mechanism == "computer_use"
    assert result.steps >= 1
    assert result.detail_text == "All done!"

    # The left_click action should have been dispatched.
    click_calls = [c for c in fake_cg.calls if c[0] == "click"]
    assert len(click_calls) >= 1
    assert click_calls[0][1] == 100  # x
    assert click_calls[0][2] == 200  # y

    # Should have emitted ProgressEvents.
    assert len(events) > 0
    kinds = {e.kind for e in events}
    assert "step" in kinds or "tool_call" in kinds


# ---------------------------------------------------------------------------
# Test: execute wraps streaming (no on_event)
# ---------------------------------------------------------------------------

def test_execute_calls_streaming(monkeypatch):
    _patch_cgevent(monkeypatch)
    _patch_capture(monkeypatch)
    _patch_screen_size(monkeypatch)

    client_obj = make_scripted_client()
    c = ComputerUseConnector(
        client_factory=lambda: client_obj,
        screen_size=(1440, 900),
    )
    result = c.execute(Intent("agent_task", raw_utterance="do something"))
    assert result.ok is True


# ---------------------------------------------------------------------------
# Test: client unavailable → ConnectorResult ok=False
# ---------------------------------------------------------------------------

def test_client_error_returns_not_ok(monkeypatch):
    _patch_capture(monkeypatch)

    def bad_factory():
        raise RuntimeError("no key")

    c = ComputerUseConnector(client_factory=bad_factory, screen_size=(1440, 900))
    result = c.execute(Intent("agent_task"))
    assert result.ok is False
    assert result.error == "client_unavailable"


# ---------------------------------------------------------------------------
# Test: max_steps guard — loop terminates even with a non-stop client
# ---------------------------------------------------------------------------

def test_max_steps_terminates(monkeypatch):
    _patch_cgevent(monkeypatch)
    _patch_capture(monkeypatch)
    _patch_screen_size(monkeypatch)

    # Build a client that ALWAYS returns tool_use to force max_steps.
    class InfiniteClient:
        call_count = 0
        class _Beta:
            def __init__(self, outer): self._outer = outer
            class _Messages:
                def __init__(self, outer): self._outer = outer
                def create(self, **kwargs):
                    self._outer._outer.call_count += 1
                    return _make_response(
                        [_make_tool_block("tu_inf", {"action": "screenshot"})],
                        "tool_use",
                    )
            @property
            def messages(self): return self._Messages(self)
        @property
        def beta(self): return self._Beta(self)

    client_obj = InfiniteClient()
    c = ComputerUseConnector(
        client_factory=lambda: client_obj,
        screen_size=(1440, 900),
        max_steps=3,
    )
    result = c.execute(Intent("agent_task", raw_utterance="loop forever"))
    # Must terminate and not raise.
    assert isinstance(result, ConnectorResult)
    assert client_obj.call_count <= 3


# ---------------------------------------------------------------------------
# Test: never raises (exception inside dispatch)
# ---------------------------------------------------------------------------

def test_execute_never_raises_on_exception(monkeypatch):
    _patch_capture(monkeypatch)
    _patch_screen_size(monkeypatch)

    # Client raises mid-loop.
    class ExplodingClient:
        class _Beta:
            class _Messages:
                def create(self, **kwargs):
                    raise RuntimeError("network error")
            @property
            def messages(self): return self._Messages()
        @property
        def beta(self): return self._Beta()

    c = ComputerUseConnector(
        client_factory=lambda: ExplodingClient(),
        screen_size=(1440, 900),
    )
    result = c.execute(Intent("agent_task"))
    assert result.ok is False
    assert result.error == "computer_use_error"


# ---------------------------------------------------------------------------
# Test: tool_schema structure
# ---------------------------------------------------------------------------

def test_tool_schema_shape():
    c = ComputerUseConnector()
    schema = c.tool_schema()
    assert schema["name"] == "computer_use"
    assert "pixel-level" in schema["description"]
    assert "input_schema" in schema


# ---------------------------------------------------------------------------
# Test: cgevent additions — type_text and scroll are importable
# ---------------------------------------------------------------------------

def test_cgevent_type_text_exported():
    import curby_jarvis.cgevent as cg
    assert callable(getattr(cg, "type_text", None))


def test_cgevent_scroll_exported():
    import curby_jarvis.cgevent as cg
    assert callable(getattr(cg, "scroll", None))


def test_cgevent_type_text_no_text_returns_false(monkeypatch):
    """type_text('') should return False without touching Quartz."""
    import curby_jarvis.cgevent as cg
    # Patch secure_input_active to False so we can test the empty-text guard.
    from curby_jarvis.ax import secure_input
    monkeypatch.setattr(secure_input, "secure_input_active", lambda: False)
    assert cg.type_text("") is False


def test_cgevent_type_text_secure_input_returns_false(monkeypatch):
    import curby_jarvis.cgevent as cg
    # Patch the name as bound at module level in cgevent (same pattern as test_deixis_click).
    monkeypatch.setattr(cg, "secure_input_active", lambda: True)
    assert cg.type_text("hello") is False


def test_cgevent_scroll_secure_input_returns_false(monkeypatch):
    import curby_jarvis.cgevent as cg
    monkeypatch.setattr(cg, "secure_input_active", lambda: True)
    assert cg.scroll(0, 3) is False


# ---------------------------------------------------------------------------
# Test: action normalization helper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Return", "return"),
    ("Escape", "escape"),
    ("ctrl+c", "ctrl+c"),
    ("super+l", "cmd+l"),
    ("Tab", "tab"),
    ("BackSpace", "delete"),
])
def test_normalize_key(raw, expected):
    result = ComputerUseConnector._normalize_key(raw)
    assert result == expected


# ---------------------------------------------------------------------------
# Test: preview card
# ---------------------------------------------------------------------------

def test_preview_card():
    c = ComputerUseConnector()
    card = c.preview(Intent("agent_task", raw_utterance="click submit"))
    assert card.title == "computer use"
    assert card.mechanism == "computer_use"


# ---------------------------------------------------------------------------
# Test: _dispatch_action routing
# ---------------------------------------------------------------------------

def test_dispatch_action_left_click(monkeypatch):
    fake_cg = _patch_cgevent(monkeypatch)
    c = ComputerUseConnector(client_factory=lambda: None, screen_size=(1440, 900))
    result = c._dispatch_action({"action": "left_click", "coordinate": [50, 75]})
    assert result is True
    assert ("click", 50, 75) in fake_cg.calls


def test_dispatch_action_type(monkeypatch):
    fake_cg = _patch_cgevent(monkeypatch)
    c = ComputerUseConnector(client_factory=lambda: None, screen_size=(1440, 900))
    result = c._dispatch_action({"action": "type", "text": "hello world"})
    assert result is True
    assert ("type_text", "hello world") in fake_cg.calls


def test_dispatch_action_scroll(monkeypatch):
    fake_cg = _patch_cgevent(monkeypatch)
    c = ComputerUseConnector(client_factory=lambda: None, screen_size=(1440, 900))
    result = c._dispatch_action({"action": "scroll", "coordinate": [300, 400],
                                 "delta_x": 0, "delta_y": 3})
    assert result is True
    scroll_calls = [call for call in fake_cg.calls if call[0] == "scroll"]
    assert len(scroll_calls) >= 1
    assert scroll_calls[0][1] == 0   # dx
    assert scroll_calls[0][2] == 3   # dy


def test_dispatch_action_key(monkeypatch):
    fake_cg = _patch_cgevent(monkeypatch)
    c = ComputerUseConnector(client_factory=lambda: None, screen_size=(1440, 900))
    result = c._dispatch_action({"action": "key", "text": "cmd+w"})
    assert result is True
    assert ("key", "cmd+w") in fake_cg.calls


def test_dispatch_action_screenshot_returns_true(monkeypatch):
    _patch_cgevent(monkeypatch)
    c = ComputerUseConnector(client_factory=lambda: None, screen_size=(1440, 900))
    result = c._dispatch_action({"action": "screenshot"})
    assert result is True


def test_dispatch_action_unknown_returns_false(monkeypatch):
    _patch_cgevent(monkeypatch)
    c = ComputerUseConnector(client_factory=lambda: None, screen_size=(1440, 900))
    result = c._dispatch_action({"action": "totally_unknown_action"})
    assert result is False

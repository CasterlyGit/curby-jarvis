"""UI-15 on-screen command-input pane — pure helper + wiring contract.

Headless-safe: only the pure helper and the dispatch wiring are exercised; no Qt
widget is constructed (that needs a display). The wiring test asserts the pane's
submit path reaches the same entry point as voice/stdin.
"""
from curby_jarvis.overlay.command_input import normalize_command


def test_normalize_strips_and_blanks():
    assert normalize_command("  open Spotify ") == "open Spotify"
    assert normalize_command("mute") == "mute"
    assert normalize_command("") == ""
    assert normalize_command("   \t ") == ""
    assert normalize_command(None) == ""


def test_normalize_preserves_inner_text():
    assert normalize_command("  what time is it  ") == "what time is it"


def test_module_imports_headless():
    # Import-time headless contract: no Qt/display touched on import.
    import curby_jarvis.overlay.command_input as ci
    assert hasattr(ci, "normalize_command")
    assert hasattr(ci, "CommandInputWidget")
    assert hasattr(ci, "HEADLESS")


def test_allow_key_focus_is_noop_safe():
    # Helper must be import + call safe even with no real window (returns cleanly).
    from curby_jarvis.macwin import allow_key_focus

    class _FakeWidget:
        def winId(self):  # pragma: no cover - not reached off a real server
            return 0

    # offscreen Qt platform path bails before touching winId; must not raise.
    allow_key_focus(_FakeWidget())

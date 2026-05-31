"""Thin Quartz CGEvent helpers — the synthetic-input floor of the controller.

This is the CGEvent *floor*: used only when the semantic AX path (ax_bridge.press,
menu_command) has no handle on the target. Clicks move the real cursor; AX press
does not — so callers prefer press and fall here.

EVERY synthetic event is gated by secure_input_active(): password fields engage
Secure Event Input, which SILENTLY swallows synthetic keys/clicks (no error). We
return False instead of a confusing no-op so the overlay can show 'blocked'.

All pyobjc/Quartz imports are lazy so this module imports headless (CI, no
display, no permission). Coordinates are LOGICAL pixels — the same space as Qt
geometry, AX frames, and the overlay (one mapped point is clickable AND paintable).
"""
from __future__ import annotations

import time

from .ax.secure_input import secure_input_active

# Aux HID key codes carried in the NSSystemDefined / NX_KEYTYPE space. These are
# the media-transport keys; they are NOT virtual keycodes and must travel via an
# NSEvent otherEventWithType:NSEventTypeSystemDefined, not CGEventCreateKeyboard.
NX_KEYTYPE_SOUND_UP = 0
NX_KEYTYPE_SOUND_DOWN = 1
NX_KEYTYPE_MUTE = 7
NX_KEYTYPE_PLAY = 16
NX_KEYTYPE_NEXT = 17
NX_KEYTYPE_PREVIOUS = 18

_MEDIA_KEYS = {
    "play": NX_KEYTYPE_PLAY,
    "next": NX_KEYTYPE_NEXT,
    "prev": NX_KEYTYPE_PREVIOUS,
    "mute": NX_KEYTYPE_MUTE,
    "sound_up": NX_KEYTYPE_SOUND_UP,
    "sound_down": NX_KEYTYPE_SOUND_DOWN,
}

# Modifier flag bits (CGEventFlags). Kept as literals so the table is readable
# without importing Quartz at module load (headless import rule).
_FLAG_CMD = 1 << 20
_FLAG_SHIFT = 1 << 17
_FLAG_OPT = 1 << 19
_FLAG_CTRL = 1 << 18
_FLAG_FN = 1 << 23

_MOD_ALIASES = {
    "cmd": _FLAG_CMD, "command": _FLAG_CMD, "⌘": _FLAG_CMD, "meta": _FLAG_CMD,
    "shift": _FLAG_SHIFT, "⇧": _FLAG_SHIFT,
    "opt": _FLAG_OPT, "option": _FLAG_OPT, "alt": _FLAG_OPT, "⌥": _FLAG_OPT,
    "ctrl": _FLAG_CTRL, "control": _FLAG_CTRL, "^": _FLAG_CTRL,
    "fn": _FLAG_FN,
}

# Virtual keycodes (US ANSI layout) for the keys our combos actually use. We map
# by character so callers say 'cmd+w' not a magic number. Letters/digits + the
# handful of symbols the rule table can emit ("cmd+shift+]").
_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33,
    "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50, " ": 49,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
    "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126,
}


def _post(event):
    """Post a CGEvent to the HID tap (the system-wide synthetic-input stream)."""
    from Quartz import CGEventPost, kCGHIDEventTap
    CGEventPost(kCGHIDEventTap, event)


def _mouse_click(x: float, y: float, *, count: int = 1) -> bool:
    """Synthesize a left mouse-down/up at logical (x, y). count=2 -> double-click."""
    if secure_input_active():
        return False
    try:
        from Quartz import (
            CGEventCreateMouseEvent,
            CGEventSetIntegerValueField,
            kCGEventLeftMouseDown,
            kCGEventLeftMouseUp,
            kCGMouseButtonLeft,
            kCGMouseEventClickState,
        )
        pt = (float(x), float(y))
        for i in range(count):
            down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, pt, kCGMouseButtonLeft)
            up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, pt, kCGMouseButtonLeft)
            # Click-state tells the app this is the 1st/2nd click of a sequence so
            # a double-click registers as a double-click, not two singles.
            CGEventSetIntegerValueField(down, kCGMouseEventClickState, i + 1)
            CGEventSetIntegerValueField(up, kCGMouseEventClickState, i + 1)
            _post(down)
            _post(up)
        return True
    except Exception:
        return False


def click(x: float, y: float) -> bool:
    """Single left-click at logical (x, y). False if Secure Input blocks it."""
    return _mouse_click(x, y, count=1)


def double_click(x: float, y: float) -> bool:
    """Double left-click at logical (x, y). False if Secure Input blocks it."""
    return _mouse_click(x, y, count=2)


def drag(x1: float, y1: float, x2: float, y2: float, *, steps: int = 12) -> bool:
    """Press at (x1,y1), drag through interpolated points, release at (x2,y2).

    Intermediate LeftMouseDragged events are required — many targets ignore a
    teleport from down to up. False if Secure Input blocks it.
    """
    if secure_input_active():
        return False
    try:
        from Quartz import (
            CGEventCreateMouseEvent,
            kCGEventLeftMouseDown,
            kCGEventLeftMouseDragged,
            kCGEventLeftMouseUp,
            kCGMouseButtonLeft,
        )
        x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
        _post(CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, (x1, y1), kCGMouseButtonLeft))
        n = max(1, int(steps))
        for i in range(1, n + 1):
            f = i / n
            px = x1 + (x2 - x1) * f
            py = y1 + (y2 - y1) * f
            _post(CGEventCreateMouseEvent(None, kCGEventLeftMouseDragged, (px, py), kCGMouseButtonLeft))
            time.sleep(0.006)  # WHY: let the target's drag tracking keep up
        _post(CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, (x2, y2), kCGMouseButtonLeft))
        return True
    except Exception:
        return False


def _parse_combo(combo: str):
    """'cmd+shift+]' -> (flags:int, keycode:int) or None if the key is unknown."""
    if not combo:
        return None
    parts = [p.strip() for p in str(combo).split("+") if p.strip()]
    if not parts:
        return None
    flags = 0
    key = None
    for p in parts:
        low = p.lower()
        if low in _MOD_ALIASES:
            flags |= _MOD_ALIASES[low]
        else:
            key = p  # last non-modifier token is the key
    if key is None:
        return None
    kc = _KEYCODES.get(key) if len(key) > 1 else _KEYCODES.get(key.lower())
    if kc is None:
        return None
    return flags, kc


def key(combo: str) -> bool:
    """Synthesize a key chord like 'cmd+w', 'cmd+shift+]', 'escape'.

    Returns False if Secure Input is active or the combo can't be parsed.
    """
    if secure_input_active():
        return False
    parsed = _parse_combo(combo)
    if parsed is None:
        return False
    flags, kc = parsed
    try:
        from Quartz import CGEventCreateKeyboardEvent, CGEventSetFlags
        down = CGEventCreateKeyboardEvent(None, kc, True)
        up = CGEventCreateKeyboardEvent(None, kc, False)
        if flags:
            CGEventSetFlags(down, flags)
            CGEventSetFlags(up, flags)
        _post(down)
        _post(up)
        return True
    except Exception:
        return False


def type_text(text: str) -> bool:
    """Type a string by generating CGEventKeyboardSetUnicodeString events.

    Each Unicode scalar is sent as a key-down/key-up pair with virtual keycode 0
    and the Unicode payload written via CGEventKeyboardSetUnicodeString.  This
    avoids looking up every character in _KEYCODES and handles arbitrary Unicode
    (emoji, non-ASCII).  Returns False if Secure Input is active or the text is
    empty or the Quartz bridge is unavailable.

    WHY CGEventKeyboardSetUnicodeString vs a paste shortcut: the paste route
    requires clipboard ownership which would clobber the user's clipboard.  The
    per-character keyboard route lands the text at the cursor regardless of app.
    """
    if not text or secure_input_active():
        return False
    try:
        from Quartz import (
            CGEventCreateKeyboardEvent,
            CGEventKeyboardSetUnicodeString,
        )
        for ch in text:
            down = CGEventCreateKeyboardEvent(None, 0, True)
            up = CGEventCreateKeyboardEvent(None, 0, False)
            CGEventKeyboardSetUnicodeString(down, len(ch), ch)
            CGEventKeyboardSetUnicodeString(up, len(ch), ch)
            _post(down)
            _post(up)
        return True
    except Exception:
        return False


def scroll(dx: float, dy: float) -> bool:
    """Synthesize a scroll-wheel event with horizontal (dx) and vertical (dy) ticks.

    Positive dy scrolls DOWN (matches most app conventions); positive dx scrolls
    RIGHT.  Units are discrete scroll 'lines'.  Returns False if Secure Input is
    active or the Quartz bridge is unavailable.

    WHY not CGEventCreateScrollWheelEvent2: the simpler kCGScrollEventUnitLine
    path works for the macro-control use-case here; pixel-precise scrolling for
    smooth animations would need the separate axis API.
    """
    if secure_input_active():
        return False
    try:
        from Quartz import (
            CGEventCreateScrollWheelEvent,
            kCGScrollEventUnitLine,
        )
        # CGEventCreateScrollWheelEvent(source, unit, wheelCount, wheel1[, wheel2])
        # wheel1 = vertical axis (positive=down on most systems), wheel2 = horizontal
        ev = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 2,
                                           int(dy), int(dx))
        _post(ev)
        return True
    except Exception:
        return False


def media_key(name: str) -> bool:
    """Tap a media-transport key in {play,next,prev,mute,sound_up,sound_down}.

    These live in the NSSystemDefined / NX aux-key space, NOT the keyboard
    keycode space, so they travel as an NSEvent systemDefined event (subtype 8,
    AUX key) carrying the key code in the high bits of data1. Returns False if
    blocked, unknown, or the AppKit bridge is unavailable.
    """
    if secure_input_active():
        return False
    code = _MEDIA_KEYS.get((name or "").lower())
    if code is None:
        return False
    try:
        from AppKit import NSEvent
        from Quartz import CGEventPost, kCGHIDEventTap

        NSSystemDefined = 14   # NSEventType.systemDefined
        NX_SUBTYPE_AUX = 8     # aux-key (media) subtype
        for down in (True, False):
            state = 0xA if down else 0xB  # NX_KEYDOWN / NX_KEYUP nibble
            data1 = (code << 16) | (state << 8)
            ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                NSSystemDefined,
                (0.0, 0.0),
                0xA00 if down else 0xB00,
                0,
                0,
                None,
                NX_SUBTYPE_AUX,
                data1,
                -1,
            )
            CGEventPost(kCGHIDEventTap, ev.CGEvent())
        return True
    except Exception:
        return False


__all__ = ["click", "double_click", "drag", "key", "media_key", "type_text", "scroll"]

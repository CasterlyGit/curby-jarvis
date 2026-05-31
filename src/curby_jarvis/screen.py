"""Slim screen-grab for the deixis Tier-2 vision fallback.

Vendored + trimmed from curby's screen_capture.py. The ONLY consumer is
DeixisClickConnector, which — on an AX hit-test miss — grabs the pixels around
the pointer and asks the model to LABEL what's under the crosshair before any
blind click (and forces confirm). We do NOT want to depend on Qt here, so this
keeps just grab_region(x, y, radius).

mss and Pillow imports are LAZY so this module imports headless. On macOS,
Screen Recording permission must be granted or mss.grab() can wedge the process
in a kernel wait — so we preflight and raise a CLEAR error instead.
"""
from __future__ import annotations

import sys


class CaptureUnavailable(RuntimeError):
    """Raised when mss/Pillow is missing or Screen Recording perm is denied."""


def _mac_can_capture() -> bool:
    """On macOS, Screen Recording permission must be granted; if not, mss.grab()
    can hang in kernel wait — so preflight. Non-mac or un-checkable -> assume yes."""
    if sys.platform != "darwin":
        return True
    try:
        from Quartz import CGPreflightScreenCaptureAccess
        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return True  # can't check -> assume granted and let grab surface failure


def capture_available() -> bool:
    """True if a grab would plausibly succeed (deps importable + perm granted).
    Cheap probe so the connector can skip the vision tier silently."""
    try:
        import mss  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    return _mac_can_capture()


def grab_region(x: float, y: float, radius: int = 400):
    """Capture a square of `radius` px around logical (x, y), clamped to screen.

    Returns a PIL.Image (RGB). Raises CaptureUnavailable with an actionable
    message if mss/Pillow is missing or Screen Recording permission is denied —
    the connector catches this and falls back to forced-confirm-without-label.
    """
    try:
        import mss
        from PIL import Image
    except Exception as e:  # WHY: 'mss' is an optional [vision] extra
        raise CaptureUnavailable(
            "Screen capture needs the 'mss' extra and Pillow. "
            "Install with: pip install 'curby-jarvis[vision]'"
        ) from e

    if not _mac_can_capture():
        raise CaptureUnavailable(
            "Screen Recording permission not granted. Open System Settings -> "
            "Privacy & Security -> Screen Recording -> enable Python, then restart."
        )

    with mss.mss() as sct:
        full = sct.monitors[0]  # full virtual screen (offset + size)
        screen_w, screen_h = full["width"], full["height"]
        left = max(full["left"], int(x) - radius)
        top = max(full["top"], int(y) - radius)
        right = min(full["left"] + screen_w, int(x) + radius)
        bottom = min(full["top"] + screen_h, int(y) + radius)
        region = {"left": left, "top": top, "width": max(1, right - left), "height": max(1, bottom - top)}
        raw = sct.grab(region)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


__all__ = ["grab_region", "capture_available", "CaptureUnavailable"]

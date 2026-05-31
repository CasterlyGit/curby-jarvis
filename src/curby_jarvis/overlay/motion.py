"""Shared motion vocabulary (UI-14) — one easing + timing language for the HUD.

Every animated surface (reticle orb, frosted card, edge light, lock-on bracket)
pulls durations and easing from here so the whole HUD moves with a single
personality. Pure helpers (lerp / lerp_rgb / ease) are headless and unit-tested;
``curve()`` lazily imports QEasingCurve so this module stays import-safe without Qt.
"""
from __future__ import annotations

# Durations in milliseconds. Entry stays inside the sub-250ms "instant" threshold.
DURATIONS = {
    "tick": 33,             # 30fps repaint
    "card_in": 200,         # frosted card entry
    "card_out": 120,        # frosted card dismiss
    "risk_xfade": 120,      # risk-color cross-fade on the card/reticle
    "lock_converge": 180,   # targeting bracket acquisition
    "done_ripple": 420,     # success ripple burst on the orb
    "chip_count": 260,      # 'did it in Nms' count-up
    "toast": 4000,          # undo toast lifetime
    "edge_pulse": 1100,     # ambient edge-light breathe period
}

# Named easing → QEasingCurve.Type attribute name.
EASING = {
    "in": "OutCubic",
    "out": "InCubic",
    "inout": "InOutCubic",
    "spring": "OutBack",
    "linear": "Linear",
}


def curve(name: str = "in"):
    """A QEasingCurve.Type for the named easing (lazy Qt import)."""
    from PyQt6.QtCore import QEasingCurve
    return getattr(QEasingCurve.Type, EASING.get(name, "OutCubic"))


def clamp(t: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if t < lo else hi if t > hi else t


def lerp(a: float, b: float, t: float) -> float:
    """Linear interpolate a→b by t (t clamped to 0..1)."""
    t = clamp(t)
    return a + (b - a) * t


def lerp_rgb(c1: tuple, c2: tuple, t: float) -> tuple:
    """Interpolate between two RGB tuples; returns integer 0..255 channels."""
    t = clamp(t)
    return tuple(int(round(c1[i] + (c2[i] - c1[i]) * t)) for i in range(3))


def ease_out_cubic(t: float) -> float:
    """Pure OutCubic easing for code paths that animate without a Qt timeline."""
    t = clamp(t)
    return 1.0 - (1.0 - t) ** 3


__all__ = ["DURATIONS", "EASING", "curve", "clamp", "lerp", "lerp_rgb", "ease_out_cubic"]

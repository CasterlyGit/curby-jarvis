"""Adaptive ink — background-aware panel style for the Frosted Console card (UI-12).

WHY: The Frosted Console card uses a locked deep-frost panel (#1a1d27 @ alpha 244)
that guarantees text contrast on typical dark desktops. When the desktop behind
the card is bright or busy, the text still reads cleanly, but the card becomes more
visually intrusive (harsh contrast band). This module graduates the card to
background-aware ink: sample the region under the card at 2fps (off the paint
thread), compute perceptual luminance, and nudge the panel alpha and glow width
so the card stays legible while blending better over any desktop.

The 2fps sample is owned by PreviewCardWidget (via a QTimer) and passed here as
a PIL image. This module is PURE — no Qt, no QTimer, no screen capture at import
time; it only computes style from an already-grabbed image.

Adaptation rules (luminance 0=black .. 1=white):
  dark  (lum < DARK_BG):   alpha slightly reduced (more transparency) + wider glow
                            since dark desktop enhances frosted effect.
  mid   (DARK_BG..LIGHT_BG): locked defaults (no change needed).
  light (lum > LIGHT_BG):  alpha pushed up (more opaque) to keep text legible;
                            glow reduced to avoid floating feeling on bright bg.

HEADLESS: top-level imports are pure-Python. Pillow + numpy are lazy (localized
inside mean_luminance). No Qt, no screen capture, no pyobjc at import time.
"""
from __future__ import annotations

from typing import Optional, Tuple

# Locked Frosted Console panel defaults (mirror preview_card.py).
_PANEL_RGB = (0x1A, 0x1D, 0x27)
_PANEL_ALPHA = 244
# Luminance thresholds: below DARK_BG the background is "dark"; above LIGHT_BG "light".
_DARK_BG = 0.35
_LIGHT_BG = 0.65

# Alpha range for adaptation (never go below MIN_ALPHA so text stays legible)
_MIN_ALPHA = 210
_MAX_ALPHA = 252

# Glow blur radius range (pixels)
_GLOW_DARK = 42    # wider glow on dark backgrounds enhances depth
_GLOW_MID = 34     # locked default
_GLOW_LIGHT = 24   # narrower on bright to reduce visual weight


def mean_luminance(pil_image) -> float:
    """Mean perceptual luminance of a PIL image, normalized to 0..1.

    Pure (no Qt, no capture): converts to grayscale via PIL's ITU-R 601-2 luma
    transform (L = 0.299R + 0.587G + 0.114B), averages the pixels, scales 255->1.
    An all-white image returns ~1.0, all-black ~0.0 — the invariant the phase-2
    adapter keys off to decide whether the card needs lighter or darker ink.

    Accepts any PIL Image (RGB, RGBA, L, ...). Empty images return 0.0 rather
    than dividing by zero, so a degenerate grab can't crash the overlay.
    """
    gray = pil_image.convert("L")  # PIL applies the 601-2 luma weights
    w, h = gray.size
    if w == 0 or h == 0:
        return 0.0
    import numpy as np

    arr = np.asarray(gray, dtype=np.float64)
    if arr.size == 0:
        return 0.0
    return float(arr.mean()) / 255.0


class AdaptiveInk:
    """Background-aware panel style for the Frosted Console card.

    panel_style(background) takes an optional PIL image of the region under the
    card (already grabbed at 2fps by PreviewCardWidget's QTimer) and returns an
    adapted (panel_rgb, panel_alpha) tuple. When background is None or computation
    fails, the locked Frosted Console defaults are returned so the card is always
    safe to render.

    The glow_blur() method returns the recommended QGraphicsDropShadowEffect blur
    radius for the same background — darker bg gets a wider glow.
    """

    def __init__(self, dark_bg: float = _DARK_BG, light_bg: float = _LIGHT_BG) -> None:
        self.dark_bg = dark_bg
        self.light_bg = light_bg

    def classify(self, luminance: float) -> str:
        """Bucket a 0..1 luminance into 'dark' | 'mid' | 'light'."""
        if luminance <= self.dark_bg:
            return "dark"
        if luminance >= self.light_bg:
            return "light"
        return "mid"

    def panel_style(
        self, background: Optional[object] = None
    ) -> Tuple[Tuple[int, int, int], int]:
        """Return (panel_rgb, panel_alpha) adapted to the background.

        background: a PIL Image of the screen region under the card (grabbed at
        2fps by PreviewCardWidget). None or any failure → locked defaults.

        Adaptation:
          dark bg  → slightly reduced alpha (205-220) to use frosted effect more
          mid bg   → locked defaults
          light bg → raised alpha (248-252) to maintain legibility
        """
        if background is None:
            return _PANEL_RGB, _PANEL_ALPHA

        try:
            lum = mean_luminance(background)
            bucket = self.classify(lum)

            if bucket == "dark":
                # Reduce alpha slightly: deeper frost shows more on dark bg
                # Scale: lum=0 → alpha=_MIN_ALPHA, lum=DARK_BG → alpha=_PANEL_ALPHA
                t = lum / self.dark_bg if self.dark_bg > 0 else 0.0
                alpha = int(round(_MIN_ALPHA + (_PANEL_ALPHA - _MIN_ALPHA) * t))
                return _PANEL_RGB, max(_MIN_ALPHA, min(_PANEL_ALPHA, alpha))

            if bucket == "light":
                # Raise alpha: brighter bg → more opaque panel to protect text
                # Scale: lum=LIGHT_BG → alpha=_PANEL_ALPHA, lum=1 → alpha=_MAX_ALPHA
                t = (lum - self.light_bg) / (1.0 - self.light_bg) if (1.0 - self.light_bg) > 0 else 1.0
                alpha = int(round(_PANEL_ALPHA + (_MAX_ALPHA - _PANEL_ALPHA) * t))
                return _PANEL_RGB, max(_PANEL_ALPHA, min(_MAX_ALPHA, alpha))

            # mid: locked defaults
            return _PANEL_RGB, _PANEL_ALPHA

        except Exception:
            return _PANEL_RGB, _PANEL_ALPHA

    def glow_blur(self, background: Optional[object] = None) -> int:
        """Return the recommended glow blur radius (px) for this background.

        dark bg → wider glow; mid → default 34; light → narrower glow.
        """
        if background is None:
            return _GLOW_MID

        try:
            lum = mean_luminance(background)
            bucket = self.classify(lum)
            if bucket == "dark":
                t = lum / self.dark_bg if self.dark_bg > 0 else 0.0
                return int(round(_GLOW_DARK + (_GLOW_MID - _GLOW_DARK) * t))
            if bucket == "light":
                t = (lum - self.light_bg) / (1.0 - self.light_bg) if (1.0 - self.light_bg) > 0 else 1.0
                return int(round(_GLOW_MID + (_GLOW_LIGHT - _GLOW_MID) * t))
            return _GLOW_MID
        except Exception:
            return _GLOW_MID


__all__ = ["mean_luminance", "AdaptiveInk"]

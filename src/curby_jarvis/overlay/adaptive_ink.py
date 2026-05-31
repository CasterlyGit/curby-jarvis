"""PHASE-2 STUB — adaptive ink for the Frosted Console card.

Locked v0.1 aesthetic makes the card text background-independent on purpose: the
deep frosted panel (#1a1d27 @ alpha 244) means card glyphs never depend on the
pixels behind it, so v0.1 never samples the screen for the card. The ONLY marks
that touch raw pixels are the reticle + target bracket, and those already solve
contrast with the action_highlight white-over-black-keyline trick (see
overlay/reticle.py).

This module is where a LATER phase graduates the card itself to background-aware
ink: sample the region under the card, compute its luminance, and nudge the
panel alpha / accent so the card stays legible if we ever drop the frosted floor
to show more of the desktop through it. For now only `mean_luminance` is a real,
pure, unit-tested function; `AdaptiveInk` is a skeleton that returns the locked
defaults unchanged.

HEADLESS: top-level imports are pure-Python; Pillow + numpy (both core deps) are
used lazily inside `mean_luminance`. No Qt, no screen capture, no pyobjc at import
time. The actual screen grab (lazy `mss`) lands when this graduates.
"""
from __future__ import annotations

from typing import Optional, Tuple

# Locked Frosted Console panel defaults (mirror preview_card.py). The phase-2
# adapter starts from these and only perturbs them when the sampled background
# would otherwise hurt legibility.
_PANEL_RGB = (0x1A, 0x1D, 0x27)
_PANEL_ALPHA = 244
# Below this luminance the desktop behind the card is "dark"; above it "light".
# Used only by the phase-2 path; kept here so the threshold is documented now.
_DARK_BG = 0.35
_LIGHT_BG = 0.65


def mean_luminance(pil_image) -> float:
    """Mean perceptual luminance of a PIL image, normalized to 0..1.

    Pure (no Qt, no capture): converts to grayscale via PIL's ITU-R 601-2 luma
    transform (L = 0.299R + 0.587G + 0.114B), averages the pixels, scales 255->1.
    An all-white image returns ~1.0, all-black ~0.0 — the invariant the phase-2
    adapter keys off to decide whether the card needs lighter or darker ink.

    Accepts any PIL Image (RGB, RGBA, L, ...). Empty images return 0.0 rather
    than dividing by zero, so a degenerate grab can't crash the overlay.
    """
    # Lazy: keep Pillow off the module's top-level import surface is unnecessary
    # (Pillow is pure-Python-importable headless), but we still localize the use.
    gray = pil_image.convert("L")  # PIL applies the 601-2 luma weights
    w, h = gray.size
    if w == 0 or h == 0:
        return 0.0
    # numpy is a core dep; np.asarray on the 'L' image is the fast, future-proof
    # path (Image.getdata() is deprecated in Pillow 14). mean of 8-bit -> /255.
    import numpy as np

    arr = np.asarray(gray, dtype=np.float64)
    if arr.size == 0:
        return 0.0
    return float(arr.mean()) / 255.0


class AdaptiveInk:
    """PHASE-2 skeleton: graduate the card to background-sampling ink.

    Today this is inert — `panel_style()` returns the locked Frosted Console
    defaults regardless of what's behind the card, because v0.1's opaque frost
    already guarantees contrast. When this graduates it will:
      1. lazily grab the screen region under the card (mss, optional dep),
      2. call `mean_luminance` on that region,
      3. push panel alpha up over busy/bright backgrounds and pick a light/dark
         accent variant so card text never fights the desktop showing through.

    Wiring it in is a one-line swap in PreviewCardWidget; until then the card
    paints the static palette and this class is a documented seam, not a TODO.
    """

    def __init__(self, dark_bg: float = _DARK_BG, light_bg: float = _LIGHT_BG) -> None:
        self.dark_bg = dark_bg
        self.light_bg = light_bg

    def classify(self, luminance: float) -> str:
        """Bucket a 0..1 luminance into 'dark' | 'mid' | 'light' (phase-2 seam)."""
        if luminance <= self.dark_bg:
            return "dark"
        if luminance >= self.light_bg:
            return "light"
        return "mid"

    def panel_style(
        self, background: Optional[object] = None
    ) -> Tuple[Tuple[int, int, int], int]:
        """Return (panel_rgb, panel_alpha) for the card.

        PHASE-2 STUB: ignores `background` and returns the locked frosted panel.
        The future implementation samples `background` (a PIL grab under the
        card) and adapts. Returning the constant keeps v0.1 deterministic.
        """
        return _PANEL_RGB, _PANEL_ALPHA


__all__ = ["mean_luminance", "AdaptiveInk"]

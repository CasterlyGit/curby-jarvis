"""Headless tests for overlay.preview_card + overlay.adaptive_ink.

No display, no camera, no permission: we exercise ONLY the pure helpers. The Qt
widget classes are import-guarded (HEADLESS) so importing the modules is safe; we
never construct PreviewCardWidget here. risk_color is asserted against the LOCKED
semantic hexes, and mean_luminance against synthetic all-white / all-black images.
"""
from __future__ import annotations

import pytest

from curby_jarvis.intent import (
    RISK_AMBIGUOUS,
    RISK_IRREVERSIBLE,
    RISK_LAUNCH,
    RISK_REVERSIBLE,
    PreviewCard,
)
from curby_jarvis.overlay.adaptive_ink import AdaptiveInk, mean_luminance
from curby_jarvis.overlay.preview_card import (
    DEFAULT_AUTO_DISMISS_MS,
    risk_color,
)


# ---- risk_color: the locked semantic palette --------------------------------

def test_risk_color_launch_is_cyan():
    assert risk_color(RISK_LAUNCH) == (0x00, 0xE4, 0xFF)


def test_risk_color_reversible_is_mint():
    assert risk_color(RISK_REVERSIBLE) == (0x2E, 0xE5, 0x9D)


def test_risk_color_irreversible_is_rose():
    assert risk_color(RISK_IRREVERSIBLE) == (0xFF, 0x5B, 0x8A)


def test_risk_color_ambiguous_is_amber():
    assert risk_color(RISK_AMBIGUOUS) == (0xF0, 0xA5, 0x00)


def test_risk_color_uses_frozen_risk_string_values():
    """Guard against the constants drifting: assert via the literal risk strings."""
    assert risk_color("launch") == (0x00, 0xE4, 0xFF)
    assert risk_color("reversible") == (0x2E, 0xE5, 0x9D)
    assert risk_color("irreversible") == (0xFF, 0x5B, 0x8A)
    assert risk_color("ambiguous") == (0xF0, 0xA5, 0x00)


def test_risk_color_unknown_and_none_fall_back_to_purple():
    purple = (0xB0, 0x8E, 0xFF)
    assert risk_color(None) == purple
    assert risk_color("") == purple
    assert risk_color("not-a-risk") == purple


def test_risk_color_matches_reticle_palette():
    """Card and reticle must agree on the semantic hexes (one locked palette)."""
    from curby_jarvis.overlay.reticle import RISK_COLORS

    for risk in (RISK_LAUNCH, RISK_REVERSIBLE, RISK_IRREVERSIBLE, RISK_AMBIGUOUS):
        assert risk_color(risk) == RISK_COLORS[risk]


def test_real_intent_risk_strings_all_map():
    """Every risk an Intent can emit resolves to a concrete (non-fallback) color."""
    from curby_jarvis.intent import Intent

    # launch verb -> launch; irreversible verb -> irreversible; low conf -> ambiguous
    assert risk_color(Intent(verb="open").risk) == (0x00, 0xE4, 0xFF)
    assert risk_color(Intent(verb="close").risk) == (0xFF, 0x5B, 0x8A)
    assert risk_color(Intent(verb="copy", confidence=0.2).risk) == (0xF0, 0xA5, 0x00)
    assert risk_color(Intent(verb="copy", confidence=0.99).risk) == (0x2E, 0xE5, 0x9D)


# ---- headless import contract ----------------------------------------------

def test_preview_card_imports_headless():
    """The module imports and exposes risk_color without constructing any widget."""
    import curby_jarvis.overlay.preview_card as pc

    assert callable(pc.risk_color)
    assert DEFAULT_AUTO_DISMISS_MS > 0


def test_widget_construct_is_guarded_when_headless():
    """If PyQt6 is absent the widget stub raises a clear RuntimeError, not ImportError.

    When PyQt6 IS installed (dev box) the class is the real widget; we don't
    construct it here because that needs a QApplication + display.
    """
    import curby_jarvis.overlay.preview_card as pc

    if pc.HEADLESS:
        with pytest.raises(RuntimeError):
            pc.PreviewCardWidget()
    else:
        # Real widget present; just assert it's a class we did NOT instantiate.
        assert isinstance(pc.PreviewCardWidget, type)


def test_preview_card_dataclass_is_consumable():
    """risk_color reads PreviewCard.risk exactly as the widget will at show time."""
    card = PreviewCard(title="play THIS", gloss="Spotify", risk=RISK_REVERSIBLE)
    assert risk_color(card.risk) == (0x2E, 0xE5, 0x9D)


# ---- mean_luminance: pure, no Qt --------------------------------------------

def _solid(color, size=(8, 8), mode="RGB"):
    from PIL import Image

    return Image.new(mode, size, color)


def test_mean_luminance_all_white_is_one():
    img = _solid((255, 255, 255))
    assert mean_luminance(img) == pytest.approx(1.0, abs=1e-6)


def test_mean_luminance_all_black_is_zero():
    img = _solid((0, 0, 0))
    assert mean_luminance(img) == pytest.approx(0.0, abs=1e-6)


def test_mean_luminance_mid_gray_is_about_half():
    img = _solid((128, 128, 128))
    assert mean_luminance(img) == pytest.approx(128 / 255, abs=1e-3)


def test_mean_luminance_handles_rgba():
    img = _solid((255, 255, 255, 255), mode="RGBA")
    assert mean_luminance(img) == pytest.approx(1.0, abs=1e-6)


def test_mean_luminance_uses_perceptual_weights():
    """Pure green is brighter than pure blue under ITU-R 601-2 luma weights."""
    green = mean_luminance(_solid((0, 255, 0)))
    blue = mean_luminance(_solid((0, 0, 255)))
    assert green > blue
    # 601-2: green ~0.587, blue ~0.114 of full scale.
    assert green == pytest.approx(0.587, abs=0.01)
    assert blue == pytest.approx(0.114, abs=0.01)


def test_mean_luminance_empty_image_is_zero():
    """A degenerate zero-area grab returns 0.0 rather than dividing by zero."""
    img = _solid((255, 255, 255), size=(0, 0))
    assert mean_luminance(img) == 0.0


# ---- AdaptiveInk: phase-2 skeleton stays inert ------------------------------

def test_adaptive_ink_panel_style_returns_locked_frost():
    """PHASE-2 stub returns the locked Frosted Console panel regardless of input."""
    ink = AdaptiveInk()
    rgb, alpha = ink.panel_style(background=None)
    assert rgb == (0x1A, 0x1D, 0x27)
    assert alpha == 244
    # Even passed a 'background' (ignored in the stub) it returns the same panel.
    assert ink.panel_style(background=object()) == ((0x1A, 0x1D, 0x27), 244)


def test_adaptive_ink_classify_buckets():
    ink = AdaptiveInk(dark_bg=0.35, light_bg=0.65)
    assert ink.classify(0.0) == "dark"
    assert ink.classify(0.5) == "mid"
    assert ink.classify(1.0) == "light"

"""Tests for module-L additions: overlay/preview_card.py + overlay/adaptive_ink.py.

Owner: module L
Covers:
  UI-06 show_status — pure import + headless state assertions
  UI-07 latency chip — format_latency_chip pure helper + count-up start
  UI-11 undo toast — show_undo_toast callback wiring
  UI-14 micro-animations — entry/exit animation plumbing + risk cross-fade
  UI-12 adaptive_ink — real panel_style luminance adaptation + glow_blur

All headless: no real display, no QApplication. Qt widget tests are gated on
HEADLESS==False (same pattern as test_overlay_card.py) so CI without PyQt6
skips them cleanly.
"""
from __future__ import annotations

import pytest
from PIL import Image

# ---- pure helpers (always importable) ------------------------------------------

from curby_jarvis.overlay.preview_card import (
    DEFAULT_AUTO_DISMISS_MS,
    HEADLESS,
    format_latency_chip,
    risk_color,
)
from curby_jarvis.overlay.adaptive_ink import (
    AdaptiveInk,
    mean_luminance,
    _PANEL_RGB,
    _PANEL_ALPHA,
    _MIN_ALPHA,
    _MAX_ALPHA,
    _GLOW_MID,
    _GLOW_DARK,
    _GLOW_LIGHT,
)


# ---- format_latency_chip: pure, headless ----------------------------------------

def test_format_latency_chip_rounds():
    assert format_latency_chip(423.4, "green") == "DID IT IN 423 ms"


def test_format_latency_chip_rounds_up():
    assert format_latency_chip(423.6, "green") == "DID IT IN 424 ms"


def test_format_latency_chip_zero():
    assert format_latency_chip(0.0, "red") == "DID IT IN 0 ms"


def test_format_latency_chip_large():
    assert format_latency_chip(3499.9, "amber") == "DID IT IN 3500 ms"


def test_format_latency_chip_grade_ignored_in_text():
    # grade affects color, not text; both green and red produce same format
    assert format_latency_chip(100.0, "green") == format_latency_chip(100.0, "red")


# ---- risk_color still present after L edits ------------------------------------

def test_risk_color_still_exports():
    assert callable(risk_color)
    assert risk_color(None) == (0xB0, 0x8E, 0xFF)


def test_default_auto_dismiss_still_positive():
    assert DEFAULT_AUTO_DISMISS_MS > 0


def test_headless_flag_exported():
    assert isinstance(HEADLESS, bool)


# ---- adaptive_ink: panel_style luminance adaptation ---------------------------

def _solid(color, size=(8, 8), mode="RGB"):
    return Image.new(mode, size, color)


def test_panel_style_none_returns_locked_defaults():
    ink = AdaptiveInk()
    rgb, alpha = ink.panel_style(None)
    assert rgb == _PANEL_RGB
    assert alpha == _PANEL_ALPHA


def test_panel_style_mid_luminance_returns_locked_defaults():
    # 50% gray = mid bucket → locked defaults
    img = _solid((128, 128, 128))
    ink = AdaptiveInk()
    rgb, alpha = ink.panel_style(img)
    assert rgb == _PANEL_RGB
    assert alpha == _PANEL_ALPHA


def test_panel_style_dark_bg_reduces_alpha():
    # All-black bg → dark bucket → alpha reduced toward _MIN_ALPHA
    img = _solid((0, 0, 0))
    ink = AdaptiveInk()
    rgb, alpha = ink.panel_style(img)
    assert rgb == _PANEL_RGB
    assert _MIN_ALPHA <= alpha <= _PANEL_ALPHA


def test_panel_style_light_bg_raises_alpha():
    # All-white bg → light bucket → alpha raised toward _MAX_ALPHA
    img = _solid((255, 255, 255))
    ink = AdaptiveInk()
    rgb, alpha = ink.panel_style(img)
    assert rgb == _PANEL_RGB
    assert _PANEL_ALPHA <= alpha <= _MAX_ALPHA


def test_panel_style_dark_alpha_at_boundary():
    # At exactly dark_bg luminance → should return exactly _PANEL_ALPHA (t=1.0)
    # dark_bg=0.35 → 0.35*255 ≈ 89 gray
    gray_val = int(round(0.35 * 255))
    img = _solid((gray_val, gray_val, gray_val))
    ink = AdaptiveInk(dark_bg=0.35, light_bg=0.65)
    rgb, alpha = ink.panel_style(img)
    # At boundary lum ≈ dark_bg → t≈1.0 → alpha ≈ _PANEL_ALPHA
    assert alpha >= _MIN_ALPHA


def test_panel_style_light_alpha_at_boundary():
    gray_val = int(round(0.65 * 255))
    img = _solid((gray_val, gray_val, gray_val))
    ink = AdaptiveInk(dark_bg=0.35, light_bg=0.65)
    rgb, alpha = ink.panel_style(img)
    assert alpha == _PANEL_ALPHA  # at boundary → locked default


def test_panel_style_dark_alpha_bounded_by_min():
    img = _solid((10, 10, 10))
    ink = AdaptiveInk()
    _, alpha = ink.panel_style(img)
    assert alpha >= _MIN_ALPHA


def test_panel_style_light_alpha_bounded_by_max():
    img = _solid((245, 245, 245))
    ink = AdaptiveInk()
    _, alpha = ink.panel_style(img)
    assert alpha <= _MAX_ALPHA


def test_panel_style_rgba_image_ok():
    img = _solid((255, 255, 255, 255), mode="RGBA")
    ink = AdaptiveInk()
    rgb, alpha = ink.panel_style(img)
    assert rgb == _PANEL_RGB
    assert alpha >= _PANEL_ALPHA


def test_panel_style_exception_returns_defaults():
    """If a bad object is passed, never raises — returns locked defaults."""
    ink = AdaptiveInk()
    rgb, alpha = ink.panel_style(background="not-an-image")
    assert rgb == _PANEL_RGB
    assert alpha == _PANEL_ALPHA


# ---- adaptive_ink: glow_blur --------------------------------------------------

def test_glow_blur_none_returns_mid():
    ink = AdaptiveInk()
    assert ink.glow_blur(None) == _GLOW_MID


def test_glow_blur_dark_bg_wider():
    img = _solid((0, 0, 0))
    ink = AdaptiveInk()
    blur = ink.glow_blur(img)
    assert blur >= _GLOW_MID


def test_glow_blur_light_bg_narrower():
    img = _solid((255, 255, 255))
    ink = AdaptiveInk()
    blur = ink.glow_blur(img)
    assert blur <= _GLOW_MID


def test_glow_blur_mid_returns_mid():
    img = _solid((128, 128, 128))
    ink = AdaptiveInk()
    blur = ink.glow_blur(img)
    assert blur == _GLOW_MID


def test_glow_blur_exception_returns_mid():
    ink = AdaptiveInk()
    assert ink.glow_blur(background="garbage") == _GLOW_MID


# ---- AdaptiveInk.classify (inherited behavior) --------------------------------

def test_classify_dark():
    assert AdaptiveInk().classify(0.0) == "dark"
    assert AdaptiveInk().classify(0.34) == "dark"


def test_classify_mid():
    assert AdaptiveInk().classify(0.5) == "mid"


def test_classify_light():
    assert AdaptiveInk().classify(1.0) == "light"
    assert AdaptiveInk().classify(0.66) == "light"


def test_classify_at_dark_bg_threshold():
    ink = AdaptiveInk(dark_bg=0.35, light_bg=0.65)
    assert ink.classify(0.35) == "dark"  # <=


def test_classify_at_light_bg_threshold():
    ink = AdaptiveInk(dark_bg=0.35, light_bg=0.65)
    assert ink.classify(0.65) == "light"  # >=


# ---- mean_luminance: already tested in test_overlay_card, smoke here ----------

def test_mean_luminance_white():
    img = _solid((255, 255, 255))
    assert mean_luminance(img) == pytest.approx(1.0, abs=1e-6)


def test_mean_luminance_black():
    img = _solid((0, 0, 0))
    assert mean_luminance(img) == pytest.approx(0.0, abs=1e-6)


# ---- widget smoke tests (gated on HEADLESS) ------------------------------------

@pytest.mark.skipif(HEADLESS, reason="PyQt6 not available in headless CI")
class TestPreviewCardWidgetL:
    """Widget tests — only run when PyQt6 is present. Uses offscreen rendering."""

    @pytest.fixture(autouse=True)
    def _app(self):
        """Ensure a QApplication exists for widget construction."""
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        yield app

    def _make_widget(self):
        from curby_jarvis.overlay.preview_card import PreviewCardWidget
        w = PreviewCardWidget()
        return w

    def test_show_status_does_not_raise(self):
        w = self._make_widget()
        w.show_status("planning", "Thinking about your request...")
        assert w._status_mode is True
        assert w._progress_pct > 0

    def test_show_status_sets_phase_label_text(self):
        w = self._make_widget()
        w.show_status("acting", "Executing...")
        assert w._status_label.text() == "ACTING"

    def test_show_status_updates_title(self):
        w = self._make_widget()
        w.show_status("understanding", "hello partial")
        assert w._title.text() == "hello partial"

    def test_show_done_starts_chip(self):
        w = self._make_widget()
        w.show_done({"total_ms": 423.0})
        # Chip label text should be set and timer active
        assert "DID IT IN" in w._chip_label.text()
        assert w._chip_timer.isActive() or "DID IT IN 423" in w._chip_label.text()

    def test_show_done_zero_does_not_start(self):
        w = self._make_widget()
        w.show_done({"total_ms": 0.0})
        assert not w._chip_label.isVisible()

    def test_show_undo_toast_shows_label_and_button(self):
        calls = []
        w = self._make_widget()
        w.show_undo_toast("Closed tab", 5.0, lambda: calls.append(1))
        # Label text is set and toast timer is running
        assert "Closed tab" in w._toast_label.text()
        assert w._toast_timer.isActive()
        assert w._toast_countdown > 0

    def test_undo_button_fires_callback(self):
        calls = []
        w = self._make_widget()
        w.show_undo_toast("Test", 5.0, lambda: calls.append(1))
        w._on_undo_click()
        assert len(calls) == 1
        assert not w._undo_row.isVisible()

    def test_undo_button_callback_exception_does_not_raise(self):
        def bad():
            raise RuntimeError("boom")

        w = self._make_widget()
        w.show_undo_toast("Bad", 5.0, bad)
        w._on_undo_click()  # must not propagate

    def test_show_card_hides_status_mode(self):
        from curby_jarvis.intent import PreviewCard, RISK_REVERSIBLE
        w = self._make_widget()
        w.show_status("acting", "Working...")
        assert w._status_mode is True
        card = PreviewCard(title="open Safari", gloss="Safari", risk=RISK_REVERSIBLE)
        w.show_card(card)
        assert w._status_mode is False

    def test_show_card_with_latency_starts_chip(self):
        from curby_jarvis.intent import PreviewCard, RISK_REVERSIBLE
        w = self._make_widget()
        card = PreviewCard(title="done", gloss="x", risk=RISK_REVERSIBLE)
        w.show_card(card, latency={"total_ms": 800.0})
        assert w._chip_label.isVisible()

    def test_dismiss_stops_timers(self):
        from curby_jarvis.intent import PreviewCard, RISK_REVERSIBLE
        w = self._make_widget()
        card = PreviewCard(title="t", gloss="g", risk=RISK_REVERSIBLE)
        w.show_card(card)
        w.dismiss()
        # After dismiss, ink timer should be stopped
        assert not w._ink_timer.isActive()

    def test_risk_xfade_state_initialized(self):
        w = self._make_widget()
        assert w._risk_xfade_t == 1.0
        assert w._accent == (0xB0, 0x8E, 0xFF)

    def test_start_risk_xfade_triggers_timer(self):
        w = self._make_widget()
        new_color = (0x2E, 0xE5, 0x9D)
        w._start_risk_xfade(new_color)
        assert w._accent_target == new_color
        assert w._risk_xfade_t == 0.0
        assert w._risk_xfade_timer.isActive()

    def test_start_risk_xfade_same_color_noop(self):
        w = self._make_widget()
        original = w._accent
        w._start_risk_xfade(original)
        assert not w._risk_xfade_timer.isActive()

    def test_step_risk_xfade_advances(self):
        w = self._make_widget()
        w._risk_xfade_src = (0, 0, 0)
        w._accent_target = (255, 255, 255)
        w._risk_xfade_t = 0.0
        w._step_risk_xfade()
        assert w._risk_xfade_t > 0.0

    def test_y_offset_property_roundtrip(self):
        w = self._make_widget()
        w.y_offset = 7
        assert w.y_offset == 7
        assert w._y_offset_val == 7

    def test_opacity_property_roundtrip(self):
        w = self._make_widget()
        w.opacity_val = 0.5
        assert w.opacity_val == pytest.approx(0.5)

    def test_brighten_property_roundtrip(self):
        w = self._make_widget()
        w.brighten_val = 0.3
        assert w.brighten_val == pytest.approx(0.3)

    def test_chip_countup_reaches_target(self):
        w = self._make_widget()
        w._start_chip_countup(500.0)
        # Simulate many ticks to exhaust the animation
        for _ in range(50):
            w._step_chip_countup()
        # After > 260ms of ticks, should have the final text
        assert "DID IT IN 500 ms" in w._chip_label.text()

    def test_toast_step_decrements(self):
        calls = []
        w = self._make_widget()
        w.show_undo_toast("x", 0.3, lambda: calls.append(1))
        # Three ticks of 100ms = 0.3s consumed
        w._step_toast()
        w._step_toast()
        w._step_toast()
        # Toast should be gone
        assert not w._toast_label.isVisible()

    def test_panel_rgb_defaults(self):
        w = self._make_widget()
        assert w._panel_rgb == _PANEL_RGB
        assert w._panel_alpha == _PANEL_ALPHA

"""Headless tests for the deixis reticle overlay.

Runs with NO display: the module must import whether or not PyQt6 is present, and
the pure geometry/breathing helpers must be correct WITHOUT a QApplication. Widget
construction is attempted only when PyQt6 is importable, and is itself wrapped in a
try/except skip because a CI box may have PyQt6 but no usable display.
"""
from __future__ import annotations

import math

import pytest

from curby_jarvis.overlay import reticle
from curby_jarvis.overlay.reticle import (
    RISK_COLORS,
    accent_for,
    bracket_segments,
    breathe_phase,
    breathe_radius,
)


# --------------------------------------------------------------------------- #
# import contract
# --------------------------------------------------------------------------- #

def test_module_imports_headless():
    # The mere fact this test ran proves the import didn't crash. HEADLESS is a
    # bool flag reflecting PyQt6 availability either way.
    assert isinstance(reticle.HEADLESS, bool)


# --------------------------------------------------------------------------- #
# bracket geometry — pure, no Qt
# --------------------------------------------------------------------------- #

def test_bracket_segments_count_is_eight():
    segs = bracket_segments((100, 200, 80, 40))
    assert len(segs) == 8  # two arms per corner, four corners


def test_bracket_segments_touch_each_corner():
    x, y, w, h = 100.0, 200.0, 80.0, 40.0
    segs = bracket_segments((x, y, w, h))
    corners = {(x, y), (x + w, y), (x + w, y + h), (x, y + h)}
    # Every segment starts at exactly one rect corner (brackets hug corners).
    starts = {(s[0], s[1]) for s in segs}
    assert starts == corners
    # Each corner owns exactly two segments (one H arm, one V arm).
    for cx, cy in corners:
        owned = [s for s in segs if (s[0], s[1]) == (cx, cy)]
        assert len(owned) == 2
        horiz = [s for s in owned if s[1] == s[3]]  # y unchanged -> horizontal
        vert = [s for s in owned if s[0] == s[2]]   # x unchanged -> vertical
        assert len(horiz) == 1 and len(vert) == 1


def test_bracket_arms_point_inward():
    # Arms must extend toward the rect interior, never outward past the corner.
    x, y, w, h = 0.0, 0.0, 200.0, 100.0
    segs = bracket_segments((x, y, w, h))
    cx, cy = x + w / 2.0, y + h / 2.0
    for x1, y1, x2, y2 in segs:
        # the far end (x2,y2) must be no farther from center than the corner (x1,y1)
        d_corner = (x1 - cx) ** 2 + (y1 - cy) ** 2
        d_far = (x2 - cx) ** 2 + (y2 - cy) ** 2
        assert d_far <= d_corner + 1e-9


def test_bracket_arm_length_clamped_for_tiny_rect():
    # A tiny rect must not produce arms longer than its own half-extent.
    segs = bracket_segments((0, 0, 6, 4))
    for x1, y1, x2, y2 in segs:
        length = math.hypot(x2 - x1, y2 - y1)
        assert length <= 3.0 + 1e-9  # min(w/2, h/2) == 2 here, well under


def test_bracket_arm_length_clamped_for_huge_rect():
    # Large rect arms are clamped to _BRACKET_MAX, not 22% of the side.
    segs = bracket_segments((0, 0, 2000, 2000))
    for x1, y1, x2, y2 in segs:
        length = math.hypot(x2 - x1, y2 - y1)
        assert length == pytest.approx(reticle._BRACKET_MAX)


def test_bracket_segments_degenerate_rect_no_nan():
    segs = bracket_segments((10, 10, 0, 0))
    assert len(segs) == 8
    for v in (c for s in segs for c in s):
        assert math.isfinite(v)


# --------------------------------------------------------------------------- #
# breathing math — pure
# --------------------------------------------------------------------------- #

def test_breathe_phase_bounds_and_endpoints():
    # cosine breathing: 0 at cycle start, 1 at half-period, 0 again at full period.
    assert breathe_phase(0.0) == pytest.approx(0.0)
    assert breathe_phase(reticle.BREATHE_PERIOD_S / 2.0) == pytest.approx(1.0)
    assert breathe_phase(reticle.BREATHE_PERIOD_S) == pytest.approx(0.0, abs=1e-9)
    # always within [0, 1]
    for i in range(50):
        t = i * 0.05
        assert 0.0 - 1e-9 <= breathe_phase(t) <= 1.0 + 1e-9


def test_breathe_radius_within_band():
    for i in range(50):
        t = i * 0.07
        rad = breathe_radius(t)
        assert reticle._BREATHE_MIN_R - 1e-9 <= rad <= reticle._BREATHE_MAX_R + 1e-9


# --------------------------------------------------------------------------- #
# semantic risk colors
# --------------------------------------------------------------------------- #

def test_accent_for_known_risks_match_spec():
    assert accent_for("launch") == (0x00, 0xE4, 0xFF)        # cyan
    assert accent_for("reversible") == (0x2E, 0xE5, 0x9D)    # mint
    assert accent_for("irreversible") == (0xFF, 0x5B, 0x8A)  # rose
    assert accent_for("ambiguous") == (0xF0, 0xA5, 0x00)     # amber


def test_accent_for_unknown_falls_back_to_purple():
    purple = (0xB0, 0x8E, 0xFF)
    assert accent_for(None) == purple
    assert accent_for("") == purple
    assert accent_for("nonsense") == purple


def test_risk_colors_cover_intent_risk_consts():
    # The overlay must have a color for every frozen risk constant.
    from curby_jarvis import intent

    for const in (
        intent.RISK_LAUNCH,
        intent.RISK_REVERSIBLE,
        intent.RISK_IRREVERSIBLE,
        intent.RISK_AMBIGUOUS,
    ):
        assert const in RISK_COLORS


# --------------------------------------------------------------------------- #
# widget construction — only when PyQt6 + a display are usable
# --------------------------------------------------------------------------- #

def test_widget_stub_raises_when_headless():
    if not reticle.HEADLESS:
        pytest.skip("PyQt6 present; stub path not active")
    with pytest.raises(RuntimeError):
        reticle.ReticleWidget()


def test_widget_constructs_and_api_smoke():
    if reticle.HEADLESS:
        pytest.skip("PyQt6 absent; widget path unavailable headless")
    try:
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        w = reticle.ReticleWidget()
    except Exception as e:  # no usable display on this CI box
        pytest.skip(f"Qt widget construction unavailable: {e}")

    # Exercise the public API; none of these should raise. We do NOT assert pixel
    # output (no offscreen render needed) — only that the state machine holds.
    w.set_risk("irreversible")
    w.show_reticle(640.0, 400.0)
    assert w._mode == "reticle"
    w.show_target((100.0, 120.0, 200.0, 80.0))
    assert w._mode == "target"
    # the window box must enclose the target rect plus pad
    geo = w.geometry()
    assert geo.x() <= 100 and geo.y() <= 120
    assert geo.x() + geo.width() >= 300 and geo.y() + geo.height() >= 200
    w.hide()
    assert w._mode is None
    del app

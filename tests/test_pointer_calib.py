"""Headless tests for pointer.calibration — pure affine math, no Qt / camera / perm.

We never import PyQt6: the identity fallback uses the configurable `default_size`,
and `screen_for_point` is expected to return None when no QApplication exists. The
fit/map path is exercised with hand-built corner samples so the least-squares affine
is fully covered without a display.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from curby_jarvis.pointer.calibration import (
    CORNERS,
    DEFAULT_SCREEN,
    Calibration,
    _coerce_sample,
    _fit_affine,
)

# A 1440x900 target rect at the origin; the canonical "fit [0,1]^2 -> screen" case.
W, H = 1440, 900


def _corner_samples_dict():
    """Four corners as the dict shape fit() accepts: norm point -> screen px.

    The normalized points are intentionally NOT the unit square — they simulate a
    camera FOV where the pointed corners land at an inset, rotated quad. The affine
    must recover the mapping so map() of the quad center hits the screen center.
    """
    # normalized fingertip positions when pointing at each screen corner
    norms = {
        "top_left": (0.15, 0.20),
        "top_right": (0.85, 0.18),
        "bottom_left": (0.17, 0.82),
        "bottom_right": (0.83, 0.80),
    }
    screens = {
        "top_left": (0.0, 0.0),
        "top_right": (W, 0.0),
        "bottom_left": (0.0, H),
        "bottom_right": (W, H),
    }
    return {c: {"norm": norms[c], "screen": screens[c]} for c in CORNERS}


def _unit_square_samples():
    """Clean [0,1]^2 -> 1440x900 corner samples (the spec's exact-corners case)."""
    return [
        ((0.0, 0.0), (0.0, 0.0)),
        ((1.0, 0.0), (float(W), 0.0)),
        ((0.0, 1.0), (0.0, float(H))),
        ((1.0, 1.0), (float(W), float(H))),
    ]


def test_fit_unit_square_corners_map_exactly():
    """Fitting the unit square -> screen rect maps each corner to its exact pixel."""
    cal = Calibration()
    cal.fit(_unit_square_samples())

    assert cal.map(0.0, 0.0) == pytest.approx((0.0, 0.0), abs=1e-6)
    assert cal.map(1.0, 0.0) == pytest.approx((W, 0.0), abs=1e-6)
    assert cal.map(0.0, 1.0) == pytest.approx((0.0, H), abs=1e-6)
    assert cal.map(1.0, 1.0) == pytest.approx((W, H), abs=1e-6)


def test_fit_unit_square_center():
    """The center of the unit square lands at the center of the screen rect."""
    cal = Calibration()
    cal.fit(_unit_square_samples())
    cx, cy = cal.map(0.5, 0.5)
    assert cx == pytest.approx(W / 2, abs=1e-6)
    assert cy == pytest.approx(H / 2, abs=1e-6)


def test_fit_inset_quad_recovers_center():
    """A skewed/inset pointed quad still recovers the screen center for the quad center.

    The affine is fit from a non-unit quad; the geometric center of that quad
    (mean of the four normalized corners) must map to the screen center.
    """
    cal = Calibration()
    samples = _corner_samples_dict()
    cal.fit(samples)

    norms = np.array([samples[c]["norm"] for c in CORNERS])
    qcx, qcy = norms.mean(axis=0)
    sx, sy = cal.map(qcx, qcy)
    # The four screen corners average to the screen center; an affine preserves
    # that centroid relationship, so this is exact up to lstsq float error.
    assert sx == pytest.approx(W / 2, abs=1.0)
    assert sy == pytest.approx(H / 2, abs=1.0)


def test_fit_dict_and_list_agree():
    """fit() accepts both dict and list-of-pairs shapes and yields the same matrix."""
    cal_d = Calibration()
    m_d = cal_d.fit(_unit_square_samples())  # list path

    samples = {
        "top_left": {"norm": (0.0, 0.0), "screen": (0.0, 0.0)},
        "top_right": {"norm": (1.0, 0.0), "screen": (W, 0.0)},
        "bottom_left": {"norm": (0.0, 1.0), "screen": (0.0, H)},
        "bottom_right": {"norm": (1.0, 1.0), "screen": (W, H)},
    }
    cal_l = Calibration()
    m_l = cal_l.fit(samples)  # dict path
    assert np.allclose(m_d, m_l, atol=1e-6)


def test_fit_too_few_samples_raises():
    """A half-finished calibration (<3 points) must raise, not install a degenerate fit."""
    cal = Calibration()
    with pytest.raises(ValueError):
        cal.fit([((0.0, 0.0), (0.0, 0.0)), ((1.0, 1.0), (W, H))])


def test_identity_fallback_uses_default_size_headless():
    """Uncalibrated map() stretches [0,1]^2 into default_size with no Qt available."""
    cal = Calibration(default_size=(1440, 900))
    # No matrix set -> identity fallback into default_size.
    assert cal.map(0.0, 0.0) == pytest.approx((0.0, 0.0), abs=1e-6)
    assert cal.map(1.0, 1.0) == pytest.approx((1440, 900), abs=1e-6)
    assert cal.map(0.5, 0.5) == pytest.approx((720, 450), abs=1e-6)


def test_identity_fallback_default_screen_constant():
    """With no override the fallback uses the module DEFAULT_SCREEN (headless dev size)."""
    cal = Calibration()
    w, h = DEFAULT_SCREEN
    assert cal.map(1.0, 1.0) == pytest.approx((w, h), abs=1e-6)


def test_identity_matrix_is_cached():
    """First map() caches the identity matrix so repeat calls don't re-probe Qt."""
    cal = Calibration(default_size=(1000, 800))
    assert cal.matrix is None
    cal.map(0.5, 0.5)
    assert cal.matrix is not None  # cached after first resolve


def test_save_load_roundtrip(tmp_path):
    """A fitted matrix survives save()->load() keyed by display UUID."""
    path = tmp_path / "deixis_calib.json"
    cal = Calibration(display_uuid="UUID-A", default_size=(1440, 900))
    cal.fit(_unit_square_samples())
    cal.save(path)

    loaded = Calibration.load(path, display_uuid="UUID-A")
    assert loaded.matrix is not None
    assert np.allclose(loaded.matrix, cal.matrix, atol=1e-9)
    # mapping is identical after the roundtrip
    assert loaded.map(0.5, 0.5) == pytest.approx((W / 2, H / 2), abs=1e-6)


def test_save_merges_multiple_displays(tmp_path):
    """Saving a second display merges into the same file rather than clobbering."""
    path = tmp_path / "deixis_calib.json"
    a = Calibration(display_uuid="UUID-A")
    a.fit(_unit_square_samples())
    a.save(path)

    b = Calibration(display_uuid="UUID-B")
    b.fit(_unit_square_samples())
    b.save(path)

    store = json.loads(path.read_text())
    assert set(store.keys()) == {"UUID-A", "UUID-B"}


def test_load_missing_file_is_uncalibrated(tmp_path):
    """Absent calibration file -> uncalibrated Calibration (identity fallback), no raise."""
    path = tmp_path / "does_not_exist.json"
    cal = Calibration.load(path, display_uuid="anything", default_size=(800, 600))
    assert cal.matrix is None
    assert cal.map(1.0, 1.0) == pytest.approx((800, 600), abs=1e-6)


def test_load_corrupt_file_degrades(tmp_path):
    """A corrupt JSON file must not break calibration loading."""
    path = tmp_path / "calib.json"
    path.write_text("{not valid json")
    cal = Calibration.load(path, display_uuid="x", default_size=(640, 480))
    assert cal.matrix is None
    assert cal.map(1.0, 1.0) == pytest.approx((640, 480), abs=1e-6)


def test_save_none_matrix(tmp_path):
    """An uncalibrated Calibration saves matrix=null and reloads as uncalibrated."""
    path = tmp_path / "calib.json"
    cal = Calibration(display_uuid="U")
    cal.save(path)
    store = json.loads(path.read_text())
    assert store["U"]["matrix"] is None
    reloaded = Calibration.load(path, display_uuid="U")
    assert reloaded.matrix is None


def test_screen_for_point_headless_returns_none():
    """With no QApplication, screen_for_point degrades to None (guardable headless)."""
    cal = Calibration()
    assert cal.screen_for_point(100, 100) is None


def test_coerce_sample_shapes():
    """_coerce_sample normalizes pair, norm/screen dict, and flat-dict shapes alike."""
    expect = ((0.1, 0.2), (300.0, 400.0))
    assert _coerce_sample(((0.1, 0.2), (300.0, 400.0))) == expect
    assert _coerce_sample({"norm": (0.1, 0.2), "screen": (300, 400)}) == expect
    assert _coerce_sample({"nx": 0.1, "ny": 0.2, "sx": 300, "sy": 400}) == expect
    with pytest.raises(ValueError):
        _coerce_sample({"bad": 1})


def test_fit_affine_pure_helper():
    """_fit_affine returns a 2x3 matrix in row-form [sx;sy] = M @ [nx;ny;1]."""
    src = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
    dst = np.array([[0.0, 0.0], [W, 0.0], [0.0, H], [W, H]])
    m = _fit_affine(src, dst)
    assert m.shape == (2, 3)
    out = m @ np.array([0.5, 0.5, 1.0])
    assert out == pytest.approx([W / 2, H / 2], abs=1e-6)

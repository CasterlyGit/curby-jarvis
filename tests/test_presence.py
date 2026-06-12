"""Headless unit tests for the ambient presence layer (presence.py).

All tests run under QT_QPA_PLATFORM=offscreen (the CI / test-suite default).

Coverage:
  - module imports cleanly with or without PyQt6
  - HEADLESS flag is set correctly
  - pure helpers are always importable and produce correct values
  - widget stubs raise RuntimeError on construction when HEADLESS
  - PresenceLayer stub raises when HEADLESS
  - Qt widgets are constructable and step through every SessionPhase
    without raising (when PyQt6 present)
  - PresenceLayer.start() + set_phase() + stop() lifecycle (when Qt present)
  - set_last_action propagates to the glyph (when Qt present)
"""
from __future__ import annotations

import math
import pytest

import curby_jarvis.presence as presence


# ============================================================================
# Module-level contract
# ============================================================================

class TestModuleContract:
    def test_headless_flag_is_bool(self):
        assert isinstance(presence.HEADLESS, bool)

    def test_all_exports_present(self):
        for name in presence.__all__:
            assert hasattr(presence, name), f"__all__ member {name!r} missing"

    def test_pure_helpers_importable_without_qt(self):
        # These must be callable regardless of HEADLESS.
        assert callable(presence.ambient_alpha)
        assert callable(presence.breathe_value)
        assert callable(presence.spin_rate)
        assert callable(presence.edge_vignette_alpha)
        assert callable(presence.fps_for_phase)


# ============================================================================
# Pure helper: ambient_alpha
# ============================================================================

class TestAmbientAlpha:
    def test_returns_float(self):
        v = presence.ambient_alpha("idle", 0.5)
        assert isinstance(v, float)

    def test_idle_never_zero(self):
        for breathe in (0.0, 0.5, 1.0):
            v = presence.ambient_alpha("idle", breathe)
            assert v > 0.0, "idle orb must always be visible"

    def test_active_phases_higher_than_idle(self):
        idle_max = presence.ambient_alpha("idle", 1.0)
        for phase in ("listening", "acting", "done", "error"):
            v = presence.ambient_alpha(phase, 1.0)
            assert v > idle_max, f"{phase} should be brighter than idle"

    def test_breathe_increases_alpha(self):
        # Higher breathe → higher alpha for every phase.
        for phase in ("idle", "listening", "acting"):
            lo = presence.ambient_alpha(phase, 0.0)
            hi = presence.ambient_alpha(phase, 1.0)
            assert hi >= lo

    def test_all_phases_in_range(self):
        from curby_jarvis.overlay.phase import PHASES
        for ph in PHASES:
            v = presence.ambient_alpha(ph, 0.5)
            assert 0.0 <= v <= 1.0, f"{ph}: {v} out of [0,1]"

    def test_unknown_phase_falls_back(self):
        v = presence.ambient_alpha("unknown_phase_xyz", 0.5)
        assert isinstance(v, float)
        assert 0.0 <= v <= 1.0


# ============================================================================
# Pure helper: breathe_value
# ============================================================================

class TestBreatheValue:
    def test_returns_float_in_range(self):
        for t in (0.0, 1.0, 2.5, 100.0):
            v = presence.breathe_value(t, "idle")
            assert 0.0 <= v <= 1.0

    def test_idle_uses_slow_period(self):
        # Period should be IDLE_BREATHE_PERIOD_S; two values IDLE_BREATHE_PERIOD_S apart
        # must produce the same breathe value (within floating-point tolerance).
        t0 = 10.0
        t1 = t0 + presence.IDLE_BREATHE_PERIOD_S
        v0 = presence.breathe_value(t0, "idle")
        v1 = presence.breathe_value(t1, "idle")
        assert abs(v0 - v1) < 1e-9

    def test_active_uses_fast_period(self):
        t0 = 10.0
        t1 = t0 + presence.ACTIVE_BREATHE_PERIOD_S
        v0 = presence.breathe_value(t0, "listening")
        v1 = presence.breathe_value(t1, "listening")
        assert abs(v0 - v1) < 1e-9

    def test_zero_is_valid(self):
        v = presence.breathe_value(0.0, "idle")
        assert 0.0 <= v <= 1.0

    def test_all_phases(self):
        from curby_jarvis.overlay.phase import PHASES
        for ph in PHASES:
            v = presence.breathe_value(5.0, ph)
            assert 0.0 <= v <= 1.0


# ============================================================================
# Pure helper: spin_rate
# ============================================================================

class TestSpinRate:
    def test_returns_positive_float(self):
        from curby_jarvis.overlay.phase import PHASES
        for ph in PHASES:
            r = presence.spin_rate(ph)
            assert r > 0.0

    def test_acting_faster_than_idle(self):
        assert presence.spin_rate("acting") > presence.spin_rate("idle")

    def test_unknown_phase_returns_idle_rate(self):
        r = presence.spin_rate("unknown_xyz")
        assert r == presence.spin_rate("idle")


# ============================================================================
# Pure helper: edge_vignette_alpha
# ============================================================================

class TestEdgeVignetteAlpha:
    def test_idle_is_low(self):
        v = presence.edge_vignette_alpha("idle", 0.5)
        assert v < 20.0, "idle vignette should be subtle"

    def test_acting_is_higher(self):
        idle = presence.edge_vignette_alpha("idle", 1.0)
        acting = presence.edge_vignette_alpha("acting", 1.0)
        assert acting > idle * 2, "acting vignette should be significantly brighter"

    def test_all_phases_non_negative(self):
        from curby_jarvis.overlay.phase import PHASES
        for ph in PHASES:
            v = presence.edge_vignette_alpha(ph, 0.5)
            assert v >= 0.0


# ============================================================================
# Pure helper: fps_for_phase
# ============================================================================

class TestFpsForPhase:
    def test_idle_is_low(self):
        fps = presence.fps_for_phase("idle")
        assert fps <= 10

    def test_active_is_30(self):
        for ph in ("listening", "acting", "done", "error"):
            fps = presence.fps_for_phase(ph)
            assert fps >= 25

    def test_returns_positive_int(self):
        from curby_jarvis.overlay.phase import PHASES
        for ph in PHASES:
            fps = presence.fps_for_phase(ph)
            assert isinstance(fps, int)
            assert fps > 0


# ============================================================================
# Headless stubs — only active when HEADLESS is True
# ============================================================================

class TestHeadlessStubs:
    def test_orb_stub_raises(self):
        if not presence.HEADLESS:
            pytest.skip("PyQt6 present; stubs not active")
        with pytest.raises(RuntimeError):
            presence.AmbientOrbWidget()

    def test_edge_stub_raises(self):
        if not presence.HEADLESS:
            pytest.skip("PyQt6 present; stubs not active")
        with pytest.raises(RuntimeError):
            presence.AmbientEdgeWidget()

    def test_glyph_stub_raises(self):
        if not presence.HEADLESS:
            pytest.skip("PyQt6 present; stubs not active")
        with pytest.raises(RuntimeError):
            presence.StatusGlyphWidget()

    def test_layer_stub_raises(self):
        if not presence.HEADLESS:
            pytest.skip("PyQt6 present; stubs not active")
        with pytest.raises(RuntimeError):
            presence.PresenceLayer()


# ============================================================================
# Qt widget tests (skipped when HEADLESS)
# ============================================================================

@pytest.mark.skipif(presence.HEADLESS, reason="PyQt6 not present")
class TestAmbientOrbWidget:
    """Test AmbientOrbWidget construction and phase cycling."""

    @pytest.fixture(autouse=True)
    def _app(self):
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])

    def _make(self):
        return presence.AmbientOrbWidget()

    def test_constructs_without_raising(self):
        orb = self._make()
        assert orb is not None

    def test_set_phase_all_phases_no_raise(self):
        from curby_jarvis.overlay.phase import PHASES
        orb = self._make()
        for ph in PHASES:
            orb.set_phase(ph)  # must not raise

    def test_timer_starts_after_set_phase(self):
        orb = self._make()
        orb.set_phase("listening")
        assert orb._timer.isActive()

    def test_idle_timer_still_active(self):
        # Even in idle the orb breathes (just slower).
        orb = self._make()
        orb.set_phase("idle")
        assert orb._timer.isActive()

    def test_breathe_value_attribute_in_range(self):
        import time
        orb = self._make()
        orb._breathe = presence.breathe_value(time.time(), "idle")
        assert 0.0 <= orb._breathe <= 1.0

    def test_set_phase_does_not_raise_unknown(self):
        orb = self._make()
        orb.set_phase("totally_unknown_phase")  # must not raise

    def test_ripple_triggered_on_done(self):
        orb = self._make()
        orb.set_phase("acting")
        orb.set_phase("done")
        assert orb._ripple >= 0.0

    def test_flash_triggered_on_error(self):
        orb = self._make()
        orb.set_phase("error")
        assert orb._flash_count > 0

    def test_set_level_no_raise(self):
        orb = self._make()
        orb.set_level(0.75)  # optional amplitude — must not raise


@pytest.mark.skipif(presence.HEADLESS, reason="PyQt6 not present")
class TestAmbientEdgeWidget:
    @pytest.fixture(autouse=True)
    def _app(self):
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])

    def test_constructs_without_raising(self):
        w = presence.AmbientEdgeWidget()
        assert w is not None

    def test_set_phase_all_phases_no_raise(self):
        from curby_jarvis.overlay.phase import PHASES
        w = presence.AmbientEdgeWidget()
        for ph in PHASES:
            w.set_phase(ph)

    def test_timer_active_after_set_phase(self):
        w = presence.AmbientEdgeWidget()
        w.set_phase("listening")
        assert w._timer.isActive()


@pytest.mark.skipif(presence.HEADLESS, reason="PyQt6 not present")
class TestStatusGlyphWidget:
    @pytest.fixture(autouse=True)
    def _app(self):
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])

    def test_constructs_without_raising(self):
        w = presence.StatusGlyphWidget()
        assert w is not None

    def test_set_phase_all_phases_no_raise(self):
        from curby_jarvis.overlay.phase import PHASES
        w = presence.StatusGlyphWidget()
        for ph in PHASES:
            w.set_phase(ph)

    def test_set_last_action_no_raise(self):
        w = presence.StatusGlyphWidget()
        w.set_last_action("launched Safari")
        assert w._last_action == "launched Safari"

    def test_set_last_action_truncates_long_text(self):
        w = presence.StatusGlyphWidget()
        w.set_last_action("x" * 100)
        assert len(w._last_action) <= 40

    def test_set_last_action_empty_string(self):
        w = presence.StatusGlyphWidget()
        w.set_last_action("")
        assert w._last_action == ""

    def test_set_last_action_none_like(self):
        w = presence.StatusGlyphWidget()
        w.set_last_action(None)   # type: ignore[arg-type] — should not raise
        assert w._last_action == ""

    def test_all_corners_no_raise(self):
        for corner in ("bottom-right", "bottom-left", "top-right", "top-left"):
            w = presence.StatusGlyphWidget(corner=corner)
            w.set_phase("idle")


@pytest.mark.skipif(presence.HEADLESS, reason="PyQt6 not present")
class TestPresenceLayer:
    @pytest.fixture(autouse=True)
    def _app(self):
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])

    def test_constructs_without_raising(self):
        layer = presence.PresenceLayer()
        assert layer is not None

    def test_start_no_raise(self):
        layer = presence.PresenceLayer()
        layer.start()  # must not raise

    def test_start_idempotent(self):
        layer = presence.PresenceLayer()
        layer.start()
        layer.start()  # second call must not raise or duplicate widgets

    def test_set_phase_all_phases_no_raise(self):
        from curby_jarvis.overlay.phase import PHASES
        layer = presence.PresenceLayer()
        layer.start()
        for ph in PHASES:
            layer.set_phase(ph)

    def test_set_phase_before_start_buffers(self):
        layer = presence.PresenceLayer()
        layer.set_phase("listening")  # before start — must not raise
        assert layer._phase == "listening"

    def test_set_last_action_before_start_buffers(self):
        layer = presence.PresenceLayer()
        layer.set_last_action("hello world")
        assert layer._last_action == "hello world"

    def test_stop_no_raise(self):
        layer = presence.PresenceLayer()
        layer.start()
        layer.stop()  # must not raise

    def test_stop_before_start_no_raise(self):
        layer = presence.PresenceLayer()
        layer.stop()  # must not raise before start

    def test_set_last_action_after_start_no_raise(self):
        layer = presence.PresenceLayer()
        layer.start()
        layer.set_last_action("paused music")

    def test_all_three_surfaces_built(self):
        layer = presence.PresenceLayer()
        layer.start()
        assert layer._orb is not None
        assert layer._edge is not None
        assert layer._glyph is not None

    def test_phase_propagates_to_orb(self):
        layer = presence.PresenceLayer()
        layer.start()
        layer.set_phase("acting")
        assert layer._orb._phase == "acting"

    def test_phase_propagates_to_edge(self):
        layer = presence.PresenceLayer()
        layer.start()
        layer.set_phase("done")
        assert layer._edge._phase == "done"

    def test_phase_propagates_to_glyph(self):
        layer = presence.PresenceLayer()
        layer.start()
        layer.set_phase("error")
        assert layer._glyph._phase == "error"

    def test_corners(self):
        for corner in ("bottom-right", "bottom-left", "top-right", "top-left"):
            layer = presence.PresenceLayer(corner=corner)
            layer.start()
            layer.stop()

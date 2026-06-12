"""Headless unit tests for overlay.caption, overlay.edge_light, overlay.history.

All three modules follow the reticle.py headless contract: they import cleanly
without PyQt6 or a display, and their pure helpers are testable without any
QApplication. Widget construction is attempted only when PyQt6 is present and
a display is available.

Tests here focus on the PUBLIC PURE HELPERS:
  - caption.fit_caption
  - edge_light.gradient_stops
  - history.format_row

and on the headless-import contract (HEADLESS flag, stub-raises-on-construction).
"""
from __future__ import annotations

import pytest


# ============================================================================
# overlay.caption
# ============================================================================

class TestModuleImportCaption:
    def test_imports_cleanly(self):
        import curby_jarvis.overlay.caption as cap
        assert isinstance(cap.HEADLESS, bool)

    def test_fit_caption_in_module_namespace(self):
        from curby_jarvis.overlay.caption import fit_caption
        assert callable(fit_caption)

    def test_headless_stub_raises(self):
        import curby_jarvis.overlay.caption as cap
        if not cap.HEADLESS:
            pytest.skip("PyQt6 present; stub path not active")
        with pytest.raises(RuntimeError):
            cap.CaptionWidget()


class TestFitCaption:
    def test_short_text_returned_unchanged(self):
        from curby_jarvis.overlay.caption import fit_caption
        assert fit_caption("hello world") == "hello world"

    def test_exact_max_length_unchanged(self):
        from curby_jarvis.overlay.caption import fit_caption
        s = "a" * 60
        assert fit_caption(s, 60) == s

    def test_long_text_truncated_from_left(self):
        from curby_jarvis.overlay.caption import fit_caption
        result = fit_caption("a" * 70, 60)
        assert len(result) == 60
        assert result.startswith("…")

    def test_truncated_text_ends_with_tail_of_input(self):
        from curby_jarvis.overlay.caption import fit_caption
        long = "prefix_" * 10 + "SUFFIX"
        result = fit_caption(long, 20)
        assert result.endswith("SUFFIX")

    def test_empty_text_returned_as_empty(self):
        from curby_jarvis.overlay.caption import fit_caption
        assert fit_caption("") == ""
        assert fit_caption("   ") == ""

    def test_max_chars_zero_returns_empty(self):
        from curby_jarvis.overlay.caption import fit_caption
        assert fit_caption("hello", 0) == ""

    def test_strips_leading_trailing_whitespace(self):
        from curby_jarvis.overlay.caption import fit_caption
        assert fit_caption("  hello  ") == "hello"

    def test_exactly_one_char_over_limit(self):
        from curby_jarvis.overlay.caption import fit_caption
        # 11 chars, limit 10 → 1 ellipsis + 9 tail chars
        result = fit_caption("hello world", 10)
        assert len(result) == 10
        assert result[0] == "…"
        # tail should be the last 9 chars of "hello world" = "llo world"
        assert result[1:] == "llo world"

    def test_default_max_chars_is_60(self):
        from curby_jarvis.overlay.caption import fit_caption, _MAX_CHARS_DEFAULT
        assert _MAX_CHARS_DEFAULT == 60
        long = "x" * 80
        result = fit_caption(long)
        assert len(result) == 60


# ============================================================================
# overlay.edge_light
# ============================================================================

class TestModuleImportEdgeLight:
    def test_imports_cleanly(self):
        import curby_jarvis.overlay.edge_light as el
        assert isinstance(el.HEADLESS, bool)

    def test_gradient_stops_in_module_namespace(self):
        from curby_jarvis.overlay.edge_light import gradient_stops
        assert callable(gradient_stops)

    def test_headless_stub_raises(self):
        import curby_jarvis.overlay.edge_light as el
        if not el.HEADLESS:
            pytest.skip("PyQt6 present; stub path not active")
        with pytest.raises(RuntimeError):
            el.EdgeLightWidget()


class TestGradientStops:
    def test_idle_phase_returns_all_transparent(self):
        from curby_jarvis.overlay.edge_light import gradient_stops
        stops = gradient_stops("idle", 1.0)
        for _, r, g, b, a in stops:
            assert a == 0

    def test_zero_intensity_returns_all_transparent(self):
        from curby_jarvis.overlay.edge_light import gradient_stops
        stops = gradient_stops("listening", 0.0)
        for _, r, g, b, a in stops:
            assert a == 0

    def test_listening_full_intensity_has_nonzero_alpha(self):
        from curby_jarvis.overlay.edge_light import gradient_stops
        stops = gradient_stops("listening", 1.0)
        alphas = [a for _, _, _, _, a in stops]
        assert max(alphas) > 0

    def test_stops_positions_in_0_1_range(self):
        from curby_jarvis.overlay.edge_light import gradient_stops
        stops = gradient_stops("acting", 0.8)
        for pos, *_ in stops:
            assert 0.0 <= pos <= 1.0

    def test_stops_start_at_0_end_at_1(self):
        from curby_jarvis.overlay.edge_light import gradient_stops
        stops = gradient_stops("done", 1.0)
        assert stops[0][0] == pytest.approx(0.0)
        assert stops[-1][0] == pytest.approx(1.0)

    def test_stops_rgb_values_in_byte_range(self):
        from curby_jarvis.overlay.edge_light import gradient_stops
        for phase in ("idle", "listening", "heard", "understanding", "planning", "acting", "done", "error"):
            stops = gradient_stops(phase, 0.7)
            for _, r, g, b, a in stops:
                assert 0 <= r <= 255
                assert 0 <= g <= 255
                assert 0 <= b <= 255
                assert 0 <= a <= 255

    def test_intensity_scales_peak_alpha(self):
        from curby_jarvis.overlay.edge_light import gradient_stops
        stops_low = gradient_stops("listening", 0.2)
        stops_high = gradient_stops("listening", 1.0)
        peak_low = max(a for _, _, _, _, a in stops_low)
        peak_high = max(a for _, _, _, _, a in stops_high)
        assert peak_high > peak_low

    def test_different_phases_give_different_rgb(self):
        from curby_jarvis.overlay.edge_light import gradient_stops
        s_listen = gradient_stops("listening", 1.0)
        s_error = gradient_stops("error", 1.0)
        # Extract peak-alpha stop RGB from each
        rgb_listen = next((r, g, b) for _, r, g, b, a in s_listen if a > 0)
        rgb_error = next((r, g, b) for _, r, g, b, a in s_error if a > 0)
        assert rgb_listen != rgb_error

    def test_all_known_phases_produce_stops_list(self):
        from curby_jarvis.overlay.edge_light import gradient_stops
        for phase in ("idle", "listening", "heard", "understanding", "planning", "acting", "done", "error"):
            stops = gradient_stops(phase, 0.5)
            assert isinstance(stops, list)
            assert len(stops) >= 2


# ============================================================================
# overlay.history
# ============================================================================

class TestModuleImportHistory:
    def test_imports_cleanly(self):
        import curby_jarvis.overlay.history as hist
        assert isinstance(hist.HEADLESS, bool)

    def test_format_row_in_module_namespace(self):
        from curby_jarvis.overlay.history import format_row
        assert callable(format_row)

    def test_headless_stub_raises(self):
        import curby_jarvis.overlay.history as hist
        if not hist.HEADLESS:
            pytest.skip("PyQt6 present; stub path not active")
        with pytest.raises(RuntimeError):
            hist.HistoryWidget()


class TestFormatRow:
    def test_basic_event_produces_expected_keys(self):
        from curby_jarvis.overlay.history import format_row
        ev = {"verb": "open", "target": "Spotify", "mechanism": "app_launch",
              "ok": True, "latency_ms": 350.0, "risk": "launch"}
        row = format_row(ev)
        assert "label" in row
        assert "mechanism" in row
        assert "latency_ms" in row
        assert "latency_str" in row
        assert "latency_cls" in row
        assert "risk" in row
        assert "risk_color" in row
        assert "ok" in row
        assert "ok_str" in row
        assert "undo_id" in row

    def test_label_combines_verb_and_target(self):
        from curby_jarvis.overlay.history import format_row
        row = format_row({"verb": "pause", "target": "music"})
        assert row["label"] == "pause music"

    def test_label_falls_back_to_text_field(self):
        from curby_jarvis.overlay.history import format_row
        row = format_row({"text": "fallback label"})
        assert row["label"] == "fallback label"

    def test_label_falls_back_to_event_constant(self):
        from curby_jarvis.overlay.history import format_row
        row = format_row({})
        assert row["label"] == "(event)"

    def test_ok_true_gives_checkmark(self):
        from curby_jarvis.overlay.history import format_row
        assert format_row({"ok": True})["ok_str"] == "✓"

    def test_ok_false_gives_cross(self):
        from curby_jarvis.overlay.history import format_row
        assert format_row({"ok": False})["ok_str"] == "✗"

    def test_ok_none_gives_dash(self):
        from curby_jarvis.overlay.history import format_row
        assert format_row({})["ok_str"] == "–"

    def test_latency_ms_formats_to_string(self):
        from curby_jarvis.overlay.history import format_row
        row = format_row({"latency_ms": 820.5})
        assert row["latency_str"] == "820ms"
        assert row["latency_ms"] == pytest.approx(820.5)

    def test_total_ms_preferred_over_latency_ms(self):
        from curby_jarvis.overlay.history import format_row
        row = format_row({"latency_ms": 999.0, "total_ms": 400.0})
        assert row["latency_ms"] == pytest.approx(400.0)

    def test_missing_latency_gives_dash(self):
        from curby_jarvis.overlay.history import format_row
        row = format_row({})
        assert row["latency_str"] == "–"
        assert row["latency_ms"] is None

    def test_latency_classification_green(self):
        from curby_jarvis.overlay.history import format_row
        assert format_row({"latency_ms": 200.0})["latency_cls"] == "green"

    def test_latency_classification_amber(self):
        from curby_jarvis.overlay.history import format_row
        assert format_row({"latency_ms": 1200.0})["latency_cls"] == "amber"

    def test_latency_classification_red(self):
        from curby_jarvis.overlay.history import format_row
        assert format_row({"latency_ms": 3000.0})["latency_cls"] == "red"

    def test_risk_color_known(self):
        from curby_jarvis.overlay.history import format_row, _RISK_DOT
        for risk, expected in _RISK_DOT.items():
            row = format_row({"risk": risk})
            assert row["risk_color"] == expected

    def test_risk_color_unknown_fallback(self):
        from curby_jarvis.overlay.history import format_row, _RISK_DOT_DEFAULT
        row = format_row({"risk": "banana"})
        assert row["risk_color"] == _RISK_DOT_DEFAULT

    def test_undo_id_present_when_set(self):
        from curby_jarvis.overlay.history import format_row
        row = format_row({"undo_id": "abc123"})
        assert row["undo_id"] == "abc123"

    def test_undo_id_none_when_absent(self):
        from curby_jarvis.overlay.history import format_row
        assert format_row({})["undo_id"] is None

    def test_mechanism_from_surface_fallback(self):
        from curby_jarvis.overlay.history import format_row
        row = format_row({"surface": "operational"})
        assert row["mechanism"] == "operational"

    def test_empty_event_produces_valid_row_without_raising(self):
        from curby_jarvis.overlay.history import format_row
        row = format_row({})
        assert isinstance(row, dict)

    def test_invalid_latency_gives_none(self):
        from curby_jarvis.overlay.history import format_row
        row = format_row({"latency_ms": "not-a-number"})
        assert row["latency_ms"] is None
        assert row["latency_str"] == "–"

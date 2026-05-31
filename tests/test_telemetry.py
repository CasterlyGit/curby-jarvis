"""Headless unit tests for telemetry.py and latency.py.

All tests use a tmp-file eventlog so they never touch ~/.curby/jarvis-events.jsonl
and run cleanly in CI without any native/network/Qt dependencies.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

# ── import-time sanity: no Qt / pyobjc / network should be required ──────────
import curby_jarvis.telemetry as telemetry
import curby_jarvis.latency as latency


# ─────────────────────────────────────────────────────────────────────────────
# telemetry.py
# ─────────────────────────────────────────────────────────────────────────────

class TestNewTraceId:
    def test_returns_hex_string(self):
        tid = telemetry.new_trace_id()
        assert isinstance(tid, str)
        assert len(tid) == 32
        int(tid, 16)  # must be valid hex

    def test_uniqueness(self):
        ids = {telemetry.new_trace_id() for _ in range(50)}
        assert len(ids) == 50


class TestEmit:
    def test_creates_file_and_appends(self, tmp_path):
        log = tmp_path / "events.jsonl"
        telemetry.emit(surface="operational", mechanism="test", eventlog=log)
        lines = log.read_text().splitlines()
        assert len(lines) == 1
        ev = json.loads(lines[0])
        assert ev["surface"] == "operational"
        assert ev["mechanism"] == "test"
        assert "ts" in ev

    def test_appends_multiple_lines(self, tmp_path):
        log = tmp_path / "events.jsonl"
        for i in range(5):
            telemetry.emit(surface="operational", index=i, eventlog=log)
        lines = log.read_text().splitlines()
        assert len(lines) == 5
        assert json.loads(lines[4])["index"] == 4

    def test_trace_id_written(self, tmp_path):
        log = tmp_path / "events.jsonl"
        tid = telemetry.new_trace_id()
        telemetry.emit(trace_id=tid, surface="cognitive", eventlog=log)
        ev = json.loads(log.read_text().strip())
        assert ev["trace_id"] == tid

    def test_invalid_surface_defaults_to_operational(self, tmp_path):
        log = tmp_path / "events.jsonl"
        telemetry.emit(surface="garbage", eventlog=log)
        ev = json.loads(log.read_text().strip())
        assert ev["surface"] == "operational"

    def test_valid_surfaces(self, tmp_path):
        for surf in ("operational", "cognitive", "contextual"):
            log = tmp_path / f"{surf}.jsonl"
            telemetry.emit(surface=surf, eventlog=log)
            ev = json.loads(log.read_text().strip())
            assert ev["surface"] == surf

    def test_mkdir_parents(self, tmp_path):
        log = tmp_path / "deep" / "nested" / "events.jsonl"
        telemetry.emit(surface="operational", eventlog=log)
        assert log.exists()

    def test_never_raises_on_bad_path(self):
        # Passing a non-writable path should not raise
        bad = Path("/root/cant_write_here/events.jsonl")
        telemetry.emit(surface="operational", eventlog=bad)  # must not raise

    def test_extra_fields_preserved(self, tmp_path):
        log = tmp_path / "events.jsonl"
        telemetry.emit(
            surface="cognitive",
            model="claude-haiku-4-5-20251001",
            input_tokens=42,
            output_tokens=7,
            eventlog=log,
        )
        ev = json.loads(log.read_text().strip())
        assert ev["model"] == "claude-haiku-4-5-20251001"
        assert ev["input_tokens"] == 42
        assert ev["output_tokens"] == 7


class TestReadEvents:
    def test_empty_when_file_missing(self, tmp_path):
        log = tmp_path / "missing.jsonl"
        assert telemetry.read_events(eventlog=log) == []

    def test_reads_last_n(self, tmp_path):
        log = tmp_path / "events.jsonl"
        for i in range(20):
            telemetry.emit(surface="operational", index=i, eventlog=log)
        events = telemetry.read_events(n=5, eventlog=log)
        assert len(events) == 5
        assert events[-1]["index"] == 19
        assert events[0]["index"] == 15

    def test_tolerates_bad_lines(self, tmp_path):
        log = tmp_path / "events.jsonl"
        log.write_text('{"ok": true}\n{BROKEN\n{"ok": false}\n')
        events = telemetry.read_events(eventlog=log)
        assert len(events) == 2
        assert events[0]["ok"] is True
        assert events[1]["ok"] is False

    def test_returns_all_when_fewer_than_n(self, tmp_path):
        log = tmp_path / "events.jsonl"
        telemetry.emit(surface="operational", eventlog=log)
        telemetry.emit(surface="cognitive", eventlog=log)
        events = telemetry.read_events(n=100, eventlog=log)
        assert len(events) == 2


class TestLatencyBreakdown:
    def test_total_is_sum(self):
        bd = telemetry.latency_breakdown(parse_ms=10.0, route_ms=5.0, execute_ms=800.0)
        assert bd["total_ms"] == pytest.approx(815.0)

    def test_stages_preserved(self):
        bd = telemetry.latency_breakdown(a=1.0, b=2.0)
        assert bd["a"] == 1.0
        assert bd["b"] == 2.0

    def test_empty(self):
        bd = telemetry.latency_breakdown()
        assert bd["total_ms"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# latency.py
# ─────────────────────────────────────────────────────────────────────────────

class TestGrade:
    @pytest.mark.parametrize("ms,expected", [
        (0.0,    "green"),
        (1499.9, "green"),
        (1500.0, "amber"),
        (3499.9, "amber"),
        (3500.0, "red"),
        (9999.0, "red"),
    ])
    def test_boundaries(self, ms, expected):
        assert latency.grade(ms) == expected


class TestLatencyBudget:
    def _write_events(self, path: Path, latencies: list[float]) -> None:
        """Helper: write synthetic events with latency_ms field."""
        for lat in latencies:
            telemetry.emit(
                surface="operational",
                latency_ms=lat,
                eventlog=path,
            )

    def test_p95_basic(self, tmp_path):
        log = tmp_path / "events.jsonl"
        # 100 events, values 1..100 ms
        self._write_events(log, list(range(1, 101)))
        budget = latency.LatencyBudget(eventlog=log, window=100).refresh()
        # P95 of 1..100 should be 95 (nearest-rank: ceil(95) = 95th element = 95)
        assert budget.p95_ms == pytest.approx(95.0)
        assert budget.count == 100

    def test_p95_empty_eventlog(self, tmp_path):
        log = tmp_path / "missing.jsonl"
        budget = latency.LatencyBudget(eventlog=log).refresh()
        assert budget.p95_ms == 0.0
        assert budget.count == 0

    def test_window_limits_events(self, tmp_path):
        log = tmp_path / "events.jsonl"
        # Write 50 low-latency events, then 10 high-latency events
        self._write_events(log, [10.0] * 50)
        self._write_events(log, [9000.0] * 10)
        # With window=10 only the high-latency ones are visible
        budget = latency.LatencyBudget(eventlog=log, window=10).refresh()
        assert budget.p95_ms > 1000.0
        assert budget.count == 10

    def test_regressed_true_when_over_slo(self, tmp_path):
        log = tmp_path / "events.jsonl"
        # All events well above the SLO
        self._write_events(log, [5000.0] * 20)
        budget = latency.LatencyBudget(eventlog=log).refresh()
        assert budget.regressed(threshold=0.0) is True

    def test_regressed_false_when_under_slo(self, tmp_path):
        log = tmp_path / "events.jsonl"
        self._write_events(log, [500.0] * 20)
        budget = latency.LatencyBudget(eventlog=log).refresh()
        assert budget.regressed() is False

    def test_regressed_false_with_no_events(self, tmp_path):
        log = tmp_path / "missing.jsonl"
        budget = latency.LatencyBudget(eventlog=log).refresh()
        assert budget.regressed() is False

    def test_stage_p95(self, tmp_path):
        log = tmp_path / "events.jsonl"
        for v in range(1, 11):
            telemetry.emit(
                surface="operational",
                route_ms=float(v * 10),
                eventlog=log,
            )
        budget = latency.LatencyBudget(eventlog=log, window=20).refresh()
        # P95 of [10,20,...,100] → 95th percentile
        assert budget.stage_p95("route_ms") > 0.0

    def test_stage_p95_missing_stage(self, tmp_path):
        log = tmp_path / "events.jsonl"
        self._write_events(log, [100.0])
        budget = latency.LatencyBudget(eventlog=log).refresh()
        assert budget.stage_p95("nonexistent_ms") == 0.0

    def test_refresh_returns_self(self, tmp_path):
        log = tmp_path / "events.jsonl"
        budget = latency.LatencyBudget(eventlog=log)
        result = budget.refresh()
        assert result is budget

    def test_single_event(self, tmp_path):
        log = tmp_path / "events.jsonl"
        self._write_events(log, [750.0])
        budget = latency.LatencyBudget(eventlog=log).refresh()
        assert budget.p95_ms == pytest.approx(750.0)
        assert budget.count == 1


class TestRefreshGlobal:
    def test_updates_p95_ms_module_var(self, tmp_path, monkeypatch):
        log = tmp_path / "events.jsonl"
        for v in [100.0, 200.0, 300.0]:
            telemetry.emit(surface="operational", latency_ms=v, eventlog=log)
        # Reset module-level state so test is isolated
        monkeypatch.setattr(latency, "_global_budget", None)
        result = latency.refresh_global(eventlog=log)
        assert result > 0.0
        assert latency.P95_MS == result


class TestTargets:
    def test_expected_keys_present(self):
        assert "parse_first_token_ms" in latency.TARGETS
        assert "route_ms" in latency.TARGETS
        assert "e2e_p95_ms" in latency.TARGETS

    def test_slo_values(self):
        assert latency.TARGETS["parse_first_token_ms"] == pytest.approx(400.0)
        assert latency.TARGETS["route_ms"] == pytest.approx(50.0)
        assert latency.TARGETS["e2e_p95_ms"] == pytest.approx(3500.0)

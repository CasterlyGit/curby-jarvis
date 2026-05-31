"""Headless tests for PointerStream — no real socket, no asyncio loop, no display.

Frames are fed straight into the ring via the `_ingest` seam, so we exercise the
recency / aim / confidence filtering in latest() and the hello/gesture-frame
filtering purely in-process. The ws/asyncio machinery stays untouched (lazy).
"""
from __future__ import annotations

import importlib
import time

from curby_jarvis.pointer.ws_client import PointerSample, PointerStream


def _pointer_frame(**over) -> dict:
    f = {
        "t": "pointer",
        "v": 2,
        "ts": time.time(),
        "present": True,
        "x": 0.62,
        "y": 0.41,
        "conf": 0.94,
        "aim_ok": True,
        "mirrored": True,
        "src_w": 640,
        "src_h": 480,
        "landmark": 8,
    }
    f.update(over)
    return f


# -- module hygiene ----------------------------------------------------------

def test_module_imports_headless():
    m = importlib.import_module("curby_jarvis.pointer.ws_client")
    assert hasattr(m, "PointerStream")
    assert hasattr(m, "PointerSample")


# -- ingest parses pointer frames into samples -------------------------------

def test_ingest_pointer_frame_returns_sample():
    ps = PointerStream()
    s = ps._ingest(_pointer_frame())
    assert isinstance(s, PointerSample)
    assert s.x_norm == 0.62 and s.y_norm == 0.41
    assert s.conf == 0.94 and s.aim_ok is True
    assert s.src_w == 640 and s.src_h == 480
    assert s.mirrored is True


def test_ingest_does_not_re_flip_coords():
    # Daemon already mirrored; consumer must keep x as-is (no 1-x flip).
    ps = PointerStream()
    s = ps._ingest(_pointer_frame(x=0.10))
    assert s.x_norm == 0.10  # NOT 0.90


# -- legacy + handshake frames are ignored -----------------------------------

def test_ignores_legacy_gesture_frame():
    ps = PointerStream()
    assert ps._ingest({"gesture": "tick", "ts": time.time()}) is None
    assert ps.latest() is None


def test_hello_frame_latches_geometry_but_no_sample():
    ps = PointerStream()
    out = ps._ingest({"t": "hello", "v": 2, "src_w": 1280, "src_h": 720, "mirrored": True})
    assert out is None
    assert ps.src_geometry == (1280, 720)
    assert ps.mirrored is True
    assert ps.latest() is None


def test_ignores_unknown_and_nondict_frames():
    ps = PointerStream()
    assert ps._ingest({"t": "weird"}) is None
    assert ps._ingest("not a dict") is None  # type: ignore[arg-type]
    assert ps._ingest({"v": 2}) is None       # no "t"


def test_malformed_pointer_frame_does_not_raise():
    ps = PointerStream()
    # Missing required x -> rejected, no crash, ring untouched.
    assert ps._ingest({"t": "pointer", "y": 0.5}) is None
    assert ps.latest() is None


# -- latest(): recency / aim / conf filtering --------------------------------

def test_latest_returns_fresh_aimed_confident():
    ps = PointerStream()
    ps._ingest(_pointer_frame())
    s = ps.latest(max_age_s=1.2, require_aim=True, min_conf=0.6)
    assert s is not None
    assert s.x_norm == 0.62


def test_latest_rejects_stale():
    ps = PointerStream()
    ps._ingest(_pointer_frame(ts=time.time() - 5.0))
    assert ps.latest(max_age_s=1.2) is None


def test_latest_rejects_low_confidence():
    ps = PointerStream()
    ps._ingest(_pointer_frame(conf=0.40))
    assert ps.latest(min_conf=0.6) is None


def test_latest_rejects_not_aimed_when_required():
    ps = PointerStream()
    ps._ingest(_pointer_frame(aim_ok=False))
    assert ps.latest(require_aim=True) is None
    # but with require_aim=False it comes through (still fresh + confident)
    assert ps.latest(require_aim=False) is not None


def test_latest_rejects_not_present():
    ps = PointerStream()
    ps._ingest(_pointer_frame(present=False))
    assert ps.latest() is None


def test_latest_returns_newest_qualifying():
    ps = PointerStream()
    ps._ingest(_pointer_frame(x=0.10, ts=time.time() - 0.5))
    ps._ingest(_pointer_frame(x=0.90, ts=time.time()))
    s = ps.latest()
    assert s is not None
    assert s.x_norm == 0.90  # newest wins


def test_latest_skips_stale_newest_for_fresh_older_is_not_done():
    # The ring is time-ordered; a stale newest sample short-circuits the scan
    # (we never reach into older history past a stale reading). Verifies the
    # break: a single stale sample -> None even though it's the only one.
    ps = PointerStream()
    ps._ingest(_pointer_frame(ts=time.time() - 10.0))
    assert ps.latest(max_age_s=1.2) is None


def test_ring_is_bounded():
    ps = PointerStream()
    for i in range(500):
        ps._ingest(_pointer_frame(x=i / 1000.0))
    # deque maxlen caps growth; still returns the freshest.
    assert len(ps._ring) <= 64
    assert ps.latest() is not None


# -- lifecycle is safe without a transport / before start --------------------

def test_stop_before_start_is_safe():
    ps = PointerStream()
    ps.stop()  # must not raise even though no thread exists
    assert ps.latest() is None


def test_start_noop_when_websockets_missing(monkeypatch):
    # Force the availability probe False -> start() must be a no-op (no thread).
    import curby_jarvis.pointer.ws_client as mod

    monkeypatch.setattr(mod, "_websockets_available", lambda: False)
    ps = PointerStream()
    ps.start()
    assert ps._thread is None
    assert ps.latest() is None
    ps.stop()

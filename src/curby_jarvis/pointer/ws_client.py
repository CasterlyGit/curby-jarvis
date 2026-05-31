"""PointerStream — background consumer of the hand-signal pointer websocket.

The hand-signal daemon broadcasts on ws://127.0.0.1:8765. Two frame shapes share
that socket:

    legacy gesture (byte-identical, ignored here):
        {"gesture":"tick","ts":...}
    pointer v2 (what we want):
        {"t":"pointer","v":2,"ts":...,"present":true,"x":0.62,"y":0.41,
         "conf":0.94,"aim_ok":true,"mirrored":true,"src_w":640,"src_h":480,
         "landmark":8}
    hello v2 (handshake; carries src geometry + mirror flag):
        {"t":"hello","v":2,"src_w":640,"src_h":480,"mirrored":true,...}

Coordinates are normalized [0,1] and ALREADY mirrored by the daemon (cv2.flip
runs before MediaPipe), so consumers must NOT flip again — we just record the
flag for downstream calibration.

Design constraints (HARD RULES):
  * Headless-importable: `websockets`/`asyncio` are imported LAZILY inside the
    worker thread. The module imports under CI with no network and no display.
  * If `websockets` is missing, start() is a no-op and latest() returns None, so
    deixis simply has no pointer -> intent.risk stays 'ambiguous'.
  * Reconnect with capped backoff if the daemon is down — a controller restart or
    a late-starting camera must not need a curby-jarvis restart.
  * Never raises out of the public API; the worker swallows everything and retries.

Tests feed PointerSample-shaped dicts straight into the ring via `_ingest`, so the
recency/aim/conf filtering in latest() is unit-testable with no real socket.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

DEFAULT_URL = "ws://127.0.0.1:8765"

# Ring depth: at 30 Hz this is ~1s of history — enough for FusionBinder to pick the
# freshest aimed sample near a word timestamp without unbounded growth.
_RING_MAXLEN = 64

# Reconnect backoff (seconds): start fast, cap so a down daemon doesn't spin hot.
_BACKOFF_START = 0.25
_BACKOFF_MAX = 5.0


@dataclass
class PointerSample:
    """One index-fingertip reading in normalized [0,1] camera space.

    Coords are ALREADY un-mirrored by the daemon; `mirrored` is informational so
    calibration knows the source convention. Calibration maps (x_norm,y_norm) ->
    logical screen pixels (Qt geometry == AX/CGEvent space).
    """
    x_norm: float
    y_norm: float
    ts: float
    conf: float
    aim_ok: bool
    present: bool
    src_w: int
    src_h: int
    mirrored: bool


class PointerStream:
    """Background ws consumer exposing the freshest qualifying PointerSample.

    Usage:
        ps = PointerStream()
        ps.start()
        s = ps.latest()           # None until an aimed, fresh, confident sample lands
        ...
        ps.stop()
    """

    def __init__(self, url: str = DEFAULT_URL):
        self._url = url
        self._ring: deque[PointerSample] = deque(maxlen=_RING_MAXLEN)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # src geometry + mirror flag, latched from hello/pointer frames so a caller
        # can read them even before the first qualifying sample.
        self._src_w = 0
        self._src_h = 0
        self._mirrored = True

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Spawn the consumer thread. No-op if `websockets` is unavailable or if
        already running — so a missing dep degrades to an empty stream, not a crash."""
        if self._thread is not None and self._thread.is_alive():
            return
        if not _websockets_available():
            # No transport -> latest() will keep returning None -> deixis ambiguous.
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="pointer-ws", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Signal the worker to exit and join briefly. Safe to call when not started."""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    # -- query ---------------------------------------------------------------

    def latest(
        self,
        max_age_s: float = 1.2,
        require_aim: bool = True,
        min_conf: float = 0.6,
    ) -> Optional[PointerSample]:
        """Newest sample within `max_age_s` that passes aim/conf gates, else None.

        Scans the ring newest-first and returns the first qualifying reading. A
        stale, low-confidence, not-aimed, or not-present sample is rejected, which
        is exactly what keeps a wandering hand from binding a bad coordinate.
        """
        now = time.time()
        with self._lock:
            snapshot = list(self._ring)
        for s in reversed(snapshot):
            if not s.present:
                continue
            if now - s.ts > max_age_s:
                # Ring is time-ordered; once we hit a stale one, the rest are older.
                break
            if require_aim and not s.aim_ok:
                continue
            if s.conf < min_conf:
                continue
            return s
        return None

    @property
    def src_geometry(self) -> tuple[int, int]:
        """(src_w, src_h) latched from the last hello/pointer frame, or (0,0)."""
        with self._lock:
            return (self._src_w, self._src_h)

    @property
    def mirrored(self) -> bool:
        with self._lock:
            return self._mirrored

    # -- ingest (also the headless test seam) --------------------------------

    def _ingest(self, frame: dict) -> Optional[PointerSample]:
        """Parse one decoded frame dict and, if it's a pointer frame, append a
        PointerSample to the ring. Returns the sample (or None for ignored frames).

        Ignores legacy gesture frames and the hello handshake, but latches src
        geometry + mirror flag from hello AND pointer frames. This is the single
        seam tests drive — no socket needed.
        """
        if not isinstance(frame, dict):
            return None

        t = frame.get("t")

        if t == "hello":
            with self._lock:
                self._src_w = int(frame.get("src_w", self._src_w) or 0)
                self._src_h = int(frame.get("src_h", self._src_h) or 0)
                self._mirrored = bool(frame.get("mirrored", self._mirrored))
            return None

        if t != "pointer":
            # Legacy gesture-only frame ({"gesture":...}) or anything unknown.
            return None

        try:
            src_w = int(frame.get("src_w", 0) or 0)
            src_h = int(frame.get("src_h", 0) or 0)
            mirrored = bool(frame.get("mirrored", True))
            sample = PointerSample(
                x_norm=float(frame["x"]),
                y_norm=float(frame["y"]),
                ts=float(frame.get("ts", time.time())),
                conf=float(frame.get("conf", 0.0)),
                aim_ok=bool(frame.get("aim_ok", False)),
                present=bool(frame.get("present", True)),
                src_w=src_w,
                src_h=src_h,
                mirrored=mirrored,
            )
        except (KeyError, TypeError, ValueError):
            # A malformed pointer frame must never wedge the consumer.
            return None

        with self._lock:
            if src_w:
                self._src_w = src_w
            if src_h:
                self._src_h = src_h
            self._mirrored = mirrored
            self._ring.append(sample)
        return sample

    # -- worker --------------------------------------------------------------

    def _run(self) -> None:
        """Thread entry: own a private asyncio loop and run the reconnect loop on it.

        asyncio + websockets are imported HERE so the module top-level stays pure
        Python and importable under CI with no event loop and no network.
        """
        import asyncio  # lazy: keep top-level headless

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._consume_forever())
        except Exception:
            # Defensive: the inner loop already swallows per-connection errors; this
            # is the last backstop so a worker death is silent, not a traceback.
            pass
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _consume_forever(self) -> None:
        import asyncio

        import websockets  # lazy

        backoff = _BACKOFF_START
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self._url,
                    open_timeout=3.0,
                    ping_interval=20,
                    ping_timeout=20,
                    max_queue=32,
                ) as ws:
                    backoff = _BACKOFF_START  # connected -> reset backoff
                    await self._pump(ws)
            except asyncio.CancelledError:
                break
            except Exception:
                # Daemon down / refused / dropped mid-stream — retry with backoff.
                pass

            if self._stop.is_set():
                break
            # Interruptible sleep: wake immediately on stop() rather than after the
            # full backoff, so shutdown is snappy.
            await self._sleep_or_stop(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _pump(self, ws) -> None:
        """Read frames until the socket closes or stop() is requested."""
        import asyncio
        import json

        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                # No frame this second — loop so we can observe the stop flag.
                continue
            try:
                frame = json.loads(raw)
            except (ValueError, TypeError):
                continue
            self._ingest(frame)

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep up to `seconds`, returning early if stop() fires."""
        import asyncio

        step = 0.05
        waited = 0.0
        while waited < seconds and not self._stop.is_set():
            await asyncio.sleep(step)
            waited += step


def _websockets_available() -> bool:
    """True if the optional `websockets` dep is importable. Kept lazy + cheap so the
    module imports headless even when the dep is absent."""
    import importlib.util

    return importlib.util.find_spec("websockets") is not None


__all__ = ["PointerSample", "PointerStream", "DEFAULT_URL"]

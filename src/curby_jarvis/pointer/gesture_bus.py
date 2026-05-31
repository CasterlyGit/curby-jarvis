"""GestureBus — edge-triggered named gesture pub/sub with hysteresis + cooldown.

Why this exists: the hand-signal daemon emits a raw frame for every video frame
that classifies a pose; at 30 fps that means 30 "pinch" messages per second while
the hand is pinching. Downstream (overlay, confirm, router) need exactly ONE event
per intentional gesture, not a flood that would fire 30 consecutive dispatches.

Two gates prevent flicker and re-trigger:

  Hysteresis  — a gesture is only FIRED after >=HYSTERESIS_FRAMES consecutive
                 agreeing frames (default 3). A single blip frame never fires.

  Cooldown    — after a fire, the bus ignores the same gesture kind for
                 COOLDOWN_S seconds (default 0.5s). If the user holds the pose
                 the hysteresis resets and the gesture can fire again after that
                 quiet window.

Injectable clock: pass `clock=...` to __init__ for deterministic unit tests.
Pure stdlib + dataclasses — headless, zero Qt, zero sockets.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

# All recognized gesture kinds the hand-signal daemon can report.
KINDS = (
    "pinch",
    "open_palm_stop",
    "fist",
    "swipe_left",
    "swipe_right",
    "swipe_up",
    "point",
    "thumbs_up",
)

# Minimum consecutive agreeing frames before a gesture fires.
HYSTERESIS_FRAMES: int = 3

# Seconds of quiet after a fire before the same gesture can fire again.
COOLDOWN_S: float = 0.5


@dataclass
class _GestureState:
    """Per-kind mutable tracking state.

    run_kind / run_count track the current accumulating run across all kinds
    (only one kind can be accumulating at a time — a new kind resets the run).
    last_fired_ts / last_fired_kind record the most recent edge-trigger event
    for gesture_confirm() checks.
    kind_cooldowns is a per-kind dict of the last monotonic time that kind fired,
    so different gesture kinds don't suppress each other's cooldowns.
    """
    run_kind: Optional[str] = None         # kind currently accumulating
    run_count: int = 0                     # consecutive-frame count
    last_fired_ts: float = 0.0             # monotonic ts of last fire (any kind)
    last_fired_kind: Optional[str] = None  # kind that last fired
    kind_cooldowns: dict = field(default_factory=dict)  # kind -> last fired ts


class GestureBus:
    """Pub/sub hub for named gesture events with frame hysteresis + cooldown.

    Usage::

        bus = GestureBus()

        def on_gesture(kind: str) -> None:
            print("gesture!", kind)

        bus.subscribe(on_gesture)
        # In the ws consumer loop:
        bus.feed("pinch", conf=0.92, ts=time.time())
        # ... after >=3 agreeing frames at 30 fps the subscriber fires once.

    Args:
        hysteresis: consecutive frames required before firing (default 3).
        cooldown_s: seconds to suppress the SAME kind after a fire (default 0.5).
                    Different gesture kinds have independent cooldown windows.
        clock:      callable returning monotonic seconds; default time.monotonic.
                    Inject a fake for deterministic tests.
    """

    def __init__(
        self,
        *,
        hysteresis: int = HYSTERESIS_FRAMES,
        cooldown_s: float = COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hysteresis = max(1, hysteresis)
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._state = _GestureState()
        self._subscribers: List[Callable[[str], None]] = []

    # -- subscription --------------------------------------------------------

    def subscribe(self, fn: Callable[[str], None]) -> None:
        """Register a callback invoked with the gesture kind string on every fire."""
        if fn not in self._subscribers:
            self._subscribers.append(fn)

    def unsubscribe(self, fn: Callable[[str], None]) -> None:
        """Remove a previously registered callback. Silent if not subscribed."""
        try:
            self._subscribers.remove(fn)
        except ValueError:
            pass

    # -- feed ----------------------------------------------------------------

    def feed(self, kind: str, conf: float = 1.0, ts: Optional[float] = None) -> None:
        """Ingest one frame's classification.

        Args:
            kind: gesture kind string (should be one of KINDS, but unknown kinds
                  pass through so callers don't need to pre-filter).
            conf: confidence 0..1 from the classifier (informational; stored for
                  future hysteresis-weighted logic but not gating yet).
            ts:   wall-clock timestamp of the frame (informational; cooldown uses
                  the injectable monotonic clock for correctness).
        """
        st = self._state
        now = self._clock()

        # --- hysteresis accumulation -----------------------------------------
        if kind == st.run_kind:
            st.run_count += 1
        else:
            # New kind breaks the run; start fresh.
            st.run_kind = kind
            st.run_count = 1

        # --- fire? -----------------------------------------------------------
        if st.run_count < self._hysteresis:
            return  # not enough agreeing frames yet

        # Cooldown gate: per-kind so different gesture kinds don't suppress each
        # other. Only the SAME kind is suppressed within the cooldown window.
        kind_last = st.kind_cooldowns.get(kind, 0.0)
        if now - kind_last < self._cooldown_s:
            return

        # Edge trigger: fire ONCE at the transition, then reset run so the next
        # run of agreeing frames (after cooldown) can fire again.
        st.kind_cooldowns[kind] = now
        st.last_fired_ts = now
        st.last_fired_kind = kind
        st.run_count = 0  # reset so next run must accumulate again

        self._emit(kind)

    # -- internals -----------------------------------------------------------

    def _emit(self, kind: str) -> None:
        """Notify all subscribers. Exceptions from subscribers are swallowed so
        one bad subscriber cannot wedge the bus."""
        for fn in list(self._subscribers):
            try:
                fn(kind)
            except Exception:
                pass


__all__ = ["GestureBus", "KINDS", "HYSTERESIS_FRAMES", "COOLDOWN_S"]

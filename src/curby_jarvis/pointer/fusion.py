"""Bind deixis ('this' / 'there') to a screen coordinate at speak-time.

The FusionBinder is the join point between the WORD stream (an Intent whose
needs_pointer flag is on because the utterance was deictic) and the POINTER
stream (the un-mirrored, calibrated index-fingertip from the gesture daemon).

Design rule, non-negotiable: when no fresh aimed sample exists we return the
intent UNCHANGED — pointer stays None, so Intent.risk collapses to 'ambiguous'
and the overlay forces a 'point and confirm'. We NEVER synthesize a coordinate;
a guessed click is worse than a refused one.

gesture_confirm() provides a lightweight pinch-check: after a confirming gesture
(pinch, within a freshness window) has been fed through a GestureBus, the binder
can confirm pending deictic intents without requiring a voice re-utterance.

Pure-Python and headless-importable: this module touches no socket, no Qt and
no display. It depends only on the frozen Intent contract and DUCK-TYPES the
stream (`.latest(...)`) and calibration (`.map(...)`) so it imports even while
the sibling ws_client / calibration modules are still being built in parallel.
"""
from __future__ import annotations

import time
from typing import Optional

from ..intent import Intent

# Match the WORD timestamp to a POINTER sample within this window. A deixis word
# ('this') and the aim of the hand that means it land within ~1s of each other;
# 1.2s tolerates ASR endpoint latency without binding a stale point.
DEFAULT_FRESHNESS_MS = 1200

# The pointer must be aimed (index extended, middle curled) and confidently
# tracked before we trust it as a click target. Mirror the ws_client defaults.
_MIN_CONF = 0.6

# Gesture kind that counts as a confirmation (Iron-Man pinch-to-confirm).
_CONFIRM_GESTURE = "pinch"


class FusionBinder:
    """Resolve an Intent's deixis against the freshest qualifying PointerSample.

    Args:
        stream:      anything exposing `latest(max_age_s, require_aim, min_conf)`
                     -> a PointerSample (with .x_norm/.y_norm) or None. In prod
                     this is pointer.ws_client.PointerStream; in tests a fake.
        calibration: anything exposing `map(nx, ny) -> (screen_x, screen_y)` in
                     logical px (pointer.calibration.Calibration in prod).
        freshness_ms: word<->pointer match window; mirrors DEFAULT_FRESHNESS_MS.
    """

    def __init__(self, stream, calibration, freshness_ms: int = DEFAULT_FRESHNESS_MS):
        self._stream = stream
        self._calib = calibration
        self._freshness_ms = int(freshness_ms)

    # -- public ---------------------------------------------------------------

    def gesture_confirm(self, within_s: float = 1.2) -> bool:
        """Return True if a 'pinch' gesture was fired within the last `within_s` seconds.

        Checks the GestureBus exposed on the stream (duck-typed: the stream must
        have a `.gestures` attribute with a `._state.last_fired_ts` and the bus
        must track the last 'pinch' fire time).  Falls back to False on any
        error (missing attr, absent bus) so callers degrade gracefully.

        This is used for pinch-to-confirm: after the overlay shows a 'confirm?'
        prompt the user pinches and this method gates the dispatch.

        Args:
            within_s: how recently (in monotonic seconds) a pinch must have
                      fired to count as a confirmation. Default 1.2s.
        """
        try:
            bus = getattr(self._stream, "gestures", None)
            if bus is None:
                return False
            # GestureBus stores last-fire time per-bus (not per-kind in v1).
            # The last fired gesture is the most recent one; we check both that
            # it was a pinch AND that it is fresh.
            state = getattr(bus, "_state", None)
            if state is None:
                return False
            last_fired = getattr(state, "last_fired_ts", 0.0)
            if last_fired == 0.0:
                return False
            # Only a 'pinch' counts as a confirmation gesture.
            last_kind = getattr(state, "last_fired_kind", None)
            if last_kind != _CONFIRM_GESTURE:
                return False
            # Compare against the bus's own clock so the injectable clock in tests
            # works correctly.
            now = bus._clock()
            return (now - last_fired) <= within_s
        except Exception:
            return False

    def bind(self, intent: Intent, word_ts: Optional[float] = None) -> Intent:
        """Return `intent` with its deixis point(s) bound, or unchanged.

        `word_ts` is accepted for the future word<->sample correlation; in v0.1
        freshness is enforced by `stream.latest(max_age_s=...)` against the
        sample's own ts, which is the only clock we can trust across the ws hop.

        Non-deictic intents pass straight through. A deictic intent with no fresh
        aimed sample also passes through UNCHANGED (pointer stays None -> the
        intent's risk stays 'ambiguous' -> overlay gates on 'point and confirm').
        """
        if not intent.needs_pointer:
            return intent

        sample = self._fresh_aimed_sample()
        if sample is None:
            return intent  # never guess a coordinate

        pt = self._map(sample)
        if pt is None:
            return intent  # calibration failed -> stay ambiguous, don't guess

        # For a two-point gesture ('move THIS THERE') resolve the destination
        # from the NEXT distinct stable sample. v0.1 LIMITATION: a second
        # latest() call can re-read the SAME sample if the hand hasn't moved; we
        # reject an identical point so we never collapse source==dest, but we do
        # not yet buffer the inter-word motion path. A real fix tracks the sample
        # ts at word2 once the ASR exposes per-word timing.
        if intent.args.get("two_point"):
            pt2 = self._second_point(exclude=sample)
            if pt2 is None:
                return intent  # missing destination -> ambiguous, force confirm
            return intent.bound_with(pointer=pt, pointer2=pt2)

        return intent.bound_with(pointer=pt)

    # -- internals ------------------------------------------------------------

    def _fresh_aimed_sample(self):
        """Freshest aimed, confident sample within the freshness window, or None."""
        try:
            return self._stream.latest(
                max_age_s=self._freshness_ms / 1000.0,
                require_aim=True,
                min_conf=_MIN_CONF,
            )
        except Exception:
            # A wedged/absent stream must degrade to 'no point', never crash the
            # binder — the intent then stays ambiguous and the overlay gates it.
            return None

    def _second_point(self, exclude):
        """Map the next distinct stable sample to a destination point, or None.

        Rejects a sample at the same normalized coord as `exclude` so a
        held-still hand can't bind source == destination.
        """
        sample = self._fresh_aimed_sample()
        if sample is None:
            return None
        if (getattr(sample, "x_norm", None) == getattr(exclude, "x_norm", None)
                and getattr(sample, "y_norm", None) == getattr(exclude, "y_norm", None)):
            return None  # identical to source -> not a real destination yet
        return self._map(sample)

    def _map(self, sample):
        """Calibrate a normalized sample to logical screen px, or None on failure."""
        try:
            x, y = self._calib.map(sample.x_norm, sample.y_norm)
        except Exception:
            return None
        return (x, y)

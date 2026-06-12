"""Phase-driven audio cue dispatcher (H — audio output).

WHY: the overlay emits a ``phase`` signal on every state transition; audio
feedback should follow those same transitions without app.py needing to know
which earcon maps to which phase.  PhaseAudio is the thin glue: it holds an
AudioFeedbackPlayer reference and routes HEARD→ack, DONE→done, ERROR→error, and
long PLANNING→thinking.  Every call is gated by CURBY_SOUND so CI is silent.

Public surface:
    PhaseAudio(player=None)  – create with an optional injected AudioFeedbackPlayer
    .on_phase(phase_str)     – call on every phase signal emission
"""
from __future__ import annotations

from typing import Optional


class PhaseAudio:
    """Route phase-string events to the appropriate AudioFeedbackPlayer earcon.

    Args:
        player: Optional pre-built :class:`~curby_jarvis.audio_feedback.AudioFeedbackPlayer`.
                When *None* a default instance is constructed lazily on first use.
                Inject a fake in tests.
    """

    def __init__(self, player: Optional[object] = None) -> None:
        self._player = player  # may be None; resolved lazily via _get_player()

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def on_phase(self, phase_str: str) -> None:
        """Map a phase transition to the right earcon.

        HEARD      → ack   (sub-100 ms tick confirming the utterance arrived)
        DONE       → done  (chime: action completed)
        ERROR      → error (low tone: something went wrong)
        PLANNING   → thinking (subtle cue after a short delay — only on long waits)
        All other phases are silent.
        """
        try:
            player = self._get_player()
            if player is None:
                return
            if phase_str == "heard":
                player.play_ack()
            elif phase_str == "done":
                player.play_done()
            elif phase_str == "error":
                player.play_error()
            elif phase_str in ("planning", "understanding"):
                # A thinking cue is appropriate when the system is about to do
                # something non-trivial. Keep it subtle (single pop).
                player.play_thinking()
            # listening / acting / idle → no earcon (too noisy)
        except Exception:
            pass  # best-effort: never let audio raise into the UI thread

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _get_player(self) -> Optional[object]:
        """Return the injected player, or lazily build a default one."""
        if self._player is None:
            try:
                from curby_jarvis.audio_feedback import AudioFeedbackPlayer  # lazy
                self._player = AudioFeedbackPlayer()
            except Exception:
                return None
        return self._player


__all__ = ["PhaseAudio"]

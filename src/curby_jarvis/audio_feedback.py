"""Audio feedback layer for curby-jarvis (H — audio output).

WHY this exists: voice assistants feel dead without earcons. Short on-device tones
(NSSound / AVAudioPlayer / NSBeep) give the user sub-100 ms confirmation that the
system heard, finished, or hit an error — without needing any API key or network.
AVSpeechSynthesizer streams TTS locally so detail_text from ConnectorResult can be
spoken back. All AppKit/AVFoundation imports are lazy; in CI (no Cocoa, CURBY_SOUND=""
or "0") every method is a no-op.

Public surface:
    AudioFeedbackPlayer(enabled=None) – short earcons via NSSound or NSBeep
    SentenceAggregator(speak)         – buffers streaming text, fires on sentence end
    speak_sentence(text, voice=None)  – speak a string via AVSpeechSynthesizer
"""
from __future__ import annotations

import os
import re
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sound_enabled(enabled: Optional[bool]) -> bool:
    """Resolve effective enabled flag.

    Explicit kwarg overrides; otherwise read CURBY_SOUND env (absent/empty/0 → off,
    any other value → on; default on when env is unset).
    """
    if enabled is not None:
        return bool(enabled)
    val = os.environ.get("CURBY_SOUND", "1")
    return val not in ("0", "", "false", "False")


# ---------------------------------------------------------------------------
# AudioFeedbackPlayer
# ---------------------------------------------------------------------------

class AudioFeedbackPlayer:
    """Short on-device earcons: ack (<100 ms), done, error, thinking.

    All native calls are lazy (AppKit/AVFoundation imported inside play_*) and
    wrapped in try/except so a missing framework or no audio device never raises.
    In CI set ``enabled=False`` or ``CURBY_SOUND=0``.
    """

    def __init__(self, enabled: Optional[bool] = None) -> None:
        self._enabled = _sound_enabled(enabled)

    # ------------------------------------------------------------------
    # public earcons
    # ------------------------------------------------------------------

    def play_ack(self) -> None:
        """Short tick — "heard you" (<100 ms)."""
        if not self._enabled:
            return
        self._play_system("Tink")

    def play_done(self) -> None:
        """Chime — action completed."""
        if not self._enabled:
            return
        self._play_system("Hero")

    def play_error(self) -> None:
        """Low tone — something went wrong."""
        if not self._enabled:
            return
        self._play_system("Basso")

    def play_thinking(self) -> None:
        """Subtle tick — still processing."""
        if not self._enabled:
            return
        self._play_system("Pop")

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _play_system(self, name: str) -> None:
        """Play a macOS system sound by name; fall back to NSBeep."""
        try:
            import AppKit  # type: ignore
            sound = AppKit.NSSound.soundNamed_(name)
            if sound is not None:
                sound.play()
            else:
                AppKit.NSBeep()
        except Exception:
            # No AppKit (CI) or no audio hardware — silently skip.
            pass


# ---------------------------------------------------------------------------
# SentenceAggregator
# ---------------------------------------------------------------------------

# Sentence-ending punctuation pattern — end on . ! ? when followed by whitespace
# or end-of-string (avoids splitting "e.g. foo" mid-word).
_SENTENCE_END = re.compile(r'(?<=[.!?])(?:\s+|$)')


class SentenceAggregator:
    """Buffer streaming text deltas and fire `speak` on each completed sentence.

    Usage::

        aggregator = SentenceAggregator(speak=speak_sentence)
        for delta in stream:
            aggregator.feed(delta)
        aggregator.flush()   # fire any trailing fragment
    """

    def __init__(self, speak: Callable[[str], None]) -> None:
        self._speak = speak
        self._buf: str = ""

    def feed(self, text_delta: str) -> None:
        """Accumulate *text_delta*; dispatch each complete sentence immediately."""
        self._buf += text_delta
        # Split on sentence boundaries; keep the last (possibly incomplete) fragment.
        parts = _SENTENCE_END.split(self._buf)
        for sentence in parts[:-1]:
            sentence = sentence.strip()
            if sentence:
                self._fire(sentence)
        self._buf = parts[-1]

    def flush(self) -> None:
        """Dispatch any remaining buffered text as a final sentence."""
        remainder = self._buf.strip()
        self._buf = ""
        if remainder:
            self._fire(remainder)

    def _fire(self, text: str) -> None:
        try:
            self._speak(text)
        except Exception:
            pass  # best-effort: never let a bad speak callback propagate


# ---------------------------------------------------------------------------
# speak_sentence
# ---------------------------------------------------------------------------

# Module-level list that holds AVSpeechSynthesizer instances alive until they
# finish speaking.  AVSpeechSynthesizer.speakUtterance_ is asynchronous; if the
# synthesizer is GC'd before it finishes the utterance is silently truncated.
# Entries are pruned on each call (isSpeaking == False → safe to release).
_active_synths: list = []


def speak_sentence(text: str, voice: Optional[str] = None) -> None:
    """Speak *text* on-device via AVSpeechSynthesizer (zero API, zero network).

    The import is lazy so this module loads cleanly in CI.  If AVFoundation is
    unavailable the function is a silent no-op.  Runs on the calling thread;
    callers that need async behaviour should schedule it themselves.

    Args:
        text:  The sentence to speak.
        voice: Optional BCP-47 language/voice identifier (e.g. ``"en-US"``).
               When None the system default voice is used.
    """
    if not text:
        return
    try:
        import AVFoundation  # type: ignore
        # Prune finished synthesizers before adding a new one.
        _active_synths[:] = [s for s in _active_synths if s.isSpeaking()]
        synth = AVFoundation.AVSpeechSynthesizer.alloc().init()
        utterance = AVFoundation.AVSpeechUtterance.speechUtteranceWithString_(text)
        if voice is not None:
            av_voice = AVFoundation.AVSpeechSynthesisVoice.voiceWithLanguage_(voice)
            if av_voice is not None:
                utterance.setVoice_(av_voice)
        # Keep synth alive past this function's return — speakUtterance_ is async.
        _active_synths.append(synth)
        synth.speakUtterance_(utterance)
    except Exception:
        # AVFoundation not available (CI / non-macOS) — silent no-op.
        pass


__all__ = [
    "AudioFeedbackPlayer",
    "SentenceAggregator",
    "speak_sentence",
]

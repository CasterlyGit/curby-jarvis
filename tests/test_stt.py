"""Unit tests for the voice-input layer's pure logic + headless-safe probes.

`should_endpoint` and `normalize_utterance` are the segmentation/cleanup policy
and are fully testable with no microphone, no Speech framework, and no display.
The probe functions must degrade cleanly (return, never raise) regardless of
whether pyobjc's Speech framework is present on the test box.
"""
from __future__ import annotations

from curby_jarvis import stt


# ---- should_endpoint --------------------------------------------------------

def test_no_text_never_endpoints():
    # With nothing recognized yet there is no phrase to end.
    assert stt.should_endpoint(False, 99.0, 99.0, silence_s=0.8, max_utt_s=12.0) is False


def test_silence_after_text_endpoints():
    # Transcription has been stable past the silence window -> phrase finished.
    assert stt.should_endpoint(True, 0.9, 1.0, silence_s=0.8, max_utt_s=12.0) is True


def test_active_speech_does_not_endpoint():
    # Text is still changing within the silence window -> keep listening.
    assert stt.should_endpoint(True, 0.2, 1.0, silence_s=0.8, max_utt_s=12.0) is False


def test_runon_hits_max_cap():
    # A long monologue that never pauses is cut at the hard cap.
    assert stt.should_endpoint(True, 0.1, 12.5, silence_s=0.8, max_utt_s=12.0) is True


def test_silence_boundary_is_inclusive():
    assert stt.should_endpoint(True, 0.8, 1.0, silence_s=0.8, max_utt_s=12.0) is True


# ---- normalize_utterance ----------------------------------------------------

def test_normalize_trims_and_passes_through_without_wake():
    assert stt.normalize_utterance("  open Spotify ") == "open Spotify"


def test_normalize_empty_is_none():
    assert stt.normalize_utterance("   ") is None
    assert stt.normalize_utterance("") is None


def test_wake_word_required_and_stripped():
    assert stt.normalize_utterance("curby open Spotify", wake_word="curby") == "open Spotify"


def test_wake_word_case_insensitive_and_punct_stripped():
    assert stt.normalize_utterance("Curby, mute", wake_word="curby") == "mute"


def test_wake_word_absent_rejects():
    assert stt.normalize_utterance("open Spotify", wake_word="curby") is None


def test_wake_word_alone_is_none():
    assert stt.normalize_utterance("curby", wake_word="curby") is None


# ---- headless-safe probes ---------------------------------------------------

def test_speech_framework_available_returns_bool():
    # Must answer truthfully without raising whether or not pyobjc is installed.
    assert isinstance(stt.speech_framework_available(), bool)


def test_authorization_summary_never_raises():
    # Returns a dict either way: status fields when the framework is present, or
    # an {"error": ...} marker when it is not. The point is: no exception escapes.
    summary = stt.authorization_summary()
    assert isinstance(summary, dict)
    assert ("error" in summary) or ("speech" in summary and "mic" in summary)


def test_voice_listener_constructs_headless():
    # Construction touches no native API; start() is where the mic/Speech live.
    vl = stt.VoiceListener(lambda _t: None)
    assert vl.running is False

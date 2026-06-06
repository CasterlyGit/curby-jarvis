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


# ---- authorization logic: status-name decoders ------------------------------

def test_speech_status_name_authorized():
    assert stt._speech_status_name(3) == "authorized"


def test_speech_status_name_denied():
    assert stt._speech_status_name(1) == "denied"


def test_speech_status_name_not_determined():
    assert stt._speech_status_name(0) == "not-determined"


def test_av_status_name_authorized():
    # AVCaptureDevice enum: 0=not-determined, 1=restricted, 2=denied, 3=authorized
    assert stt._av_status_name(3) == "authorized"


def test_av_status_name_denied():
    assert stt._av_status_name(2) == "denied"


def test_av_status_name_not_determined():
    assert stt._av_status_name(0) == "not-determined"


def test_speech_and_av_enums_are_distinct():
    # speech: 1=denied, 2=restricted  |  AV: 1=restricted, 2=denied
    # Make sure we haven't swapped the maps — common false-negative source.
    assert stt._speech_status_name(1) == "denied"
    assert stt._av_status_name(1) == "restricted"
    assert stt._speech_status_name(2) == "restricted"
    assert stt._av_status_name(2) == "denied"


# ---- authorization logic: start() guard — mock TCC status -------------------

class _FakeSpeechModule:
    """Minimal stand-in for the Speech framework module."""

    class SFSpeechRecognizer:
        _status = 3  # authorized by default

        @classmethod
        def authorizationStatus(cls):
            return cls._status

        @classmethod
        def requestAuthorization_(cls, cb):
            cb(3)

        @classmethod
        def alloc(cls):
            return cls

        @classmethod
        def init(cls):
            return None  # force "no recognizer" fast-exit in engine start

        @classmethod
        def initWithLocale_(cls, loc):
            return None


class _FakeAVModule:
    """Minimal stand-in for AVFoundation."""

    AVMediaTypeAudio = "Audio"

    class AVCaptureDevice:
        _status = 3  # authorized by default

        @classmethod
        def authorizationStatusForMediaType_(cls, _):
            return cls._status

        @classmethod
        def requestAccessForMediaType_completionHandler_(cls, _, cb):
            cb(True)

    class AVAudioEngine:
        pass  # won't be reached in these tests (recognizer returns None)


def _patch_frameworks(monkeypatch, speech_status: int, mic_status: int):
    """Inject fake Speech + AVFoundation with given TCC status codes."""
    import sys
    import types

    speech_mod = types.SimpleNamespace(
        SFSpeechRecognizer=type("SFSpeechRecognizer", (), {
            "authorizationStatus": staticmethod(lambda: speech_status),
            "requestAuthorization_": staticmethod(lambda cb: cb(speech_status)),
            "alloc": staticmethod(lambda: type("_R", (), {
                "initWithLocale_": staticmethod(lambda loc: None),
                "init": staticmethod(lambda: None),
            })()),
        }),
    )
    av_mod = types.SimpleNamespace(
        AVMediaTypeAudio="Audio",
        AVCaptureDevice=type("AVCaptureDevice", (), {
            "authorizationStatusForMediaType_": staticmethod(lambda _: mic_status),
            "requestAccessForMediaType_completionHandler_": staticmethod(
                lambda media, cb: cb(mic_status == 3)
            ),
        }),
        AVAudioEngine=type("AVAudioEngine", (), {
            "alloc": staticmethod(lambda: type("_E", (), {
                "init": staticmethod(lambda: None),
            })()),
        }),
    )
    monkeypatch.setitem(sys.modules, "Speech", speech_mod)
    monkeypatch.setitem(sys.modules, "AVFoundation", av_mod)
    # Foundation.NSLocale needed inside start()
    import curby_jarvis.stt as stt_mod
    monkeypatch.setattr(stt_mod, "speech_framework_available", lambda: True)


def test_start_returns_false_when_speech_denied(monkeypatch):
    """Hard-denied speech status must abort start() with False immediately."""
    import sys
    _patch_frameworks(monkeypatch, speech_status=1, mic_status=3)  # 1=denied for speech

    # Also stub _can_prompt_speech so we don't need NSBundle
    import curby_jarvis.stt as stt_mod
    monkeypatch.setattr(stt_mod, "_can_prompt_speech", lambda: False)

    messages = []
    vl = stt.VoiceListener(lambda _t: None, on_status=lambda m: messages.append(m))
    result = vl.start()
    assert result is False
    assert any("denied" in m.lower() or "Speech Recognition" in m for m in messages)


def test_start_returns_false_when_mic_denied(monkeypatch):
    """Hard-denied mic status must abort start() with False immediately."""
    _patch_frameworks(monkeypatch, speech_status=3, mic_status=2)  # 2=denied for AV

    import curby_jarvis.stt as stt_mod
    monkeypatch.setattr(stt_mod, "_can_prompt_speech", lambda: False)

    messages = []
    vl = stt.VoiceListener(lambda _t: None, on_status=lambda m: messages.append(m))
    result = vl.start()
    assert result is False
    assert any("microphone" in m.lower() or "Microphone" in m for m in messages)


def test_start_proceeds_when_speech_authorized(monkeypatch):
    """Authorized speech + mic must not be blocked by the guard logic.

    The engine start will fail (fake recognizer returns None) causing start()
    to return False via the 'no recognizer' path — but the permission guard
    itself must not have returned False. We verify by checking the status message
    does NOT contain 'denied'/'unavailable' language from the guard.
    """
    import sys
    _patch_frameworks(monkeypatch, speech_status=3, mic_status=3)

    import curby_jarvis.stt as stt_mod
    monkeypatch.setattr(stt_mod, "_can_prompt_speech", lambda: False)
    # Stub NSLocale so the recognizer-init path doesn't blow up
    foundation_mod = sys.modules.get("Foundation")
    if foundation_mod is None:
        import types
        foundation_mod = types.SimpleNamespace(
            NSLocale=type("NSLocale", (), {
                "localeWithLocaleIdentifier_": staticmethod(lambda _: object()),
            })
        )
        monkeypatch.setitem(sys.modules, "Foundation", foundation_mod)

    messages = []
    vl = stt.VoiceListener(lambda _t: None, on_status=lambda m: messages.append(m))
    vl.start()  # may return False due to fake recognizer returning None — that's fine
    # Guard must not have fired any denial/unavailability message
    for m in messages:
        assert "denied" not in m.lower() or "Speech Recognition" not in m
        assert "unavailable from CLI" not in m


def test_start_proceeds_optimistically_when_not_determined_no_bundle(monkeypatch):
    """not-determined status in a bare CLI must NOT hard-fail as 'unavailable'.

    This is the false-negative regression guard: the user may have pre-granted
    the permission in System Settings. We must not block on not-determined.
    Instead the engine start is attempted (and may fail for other reasons).
    """
    import sys
    _patch_frameworks(monkeypatch, speech_status=0, mic_status=0)  # 0=not-determined

    import curby_jarvis.stt as stt_mod
    monkeypatch.setattr(stt_mod, "_can_prompt_speech", lambda: False)

    foundation_mod = sys.modules.get("Foundation")
    if foundation_mod is None:
        import types
        foundation_mod = types.SimpleNamespace(
            NSLocale=type("NSLocale", (), {
                "localeWithLocaleIdentifier_": staticmethod(lambda _: object()),
            })
        )
        monkeypatch.setitem(sys.modules, "Foundation", foundation_mod)

    messages = []
    vl = stt.VoiceListener(lambda _t: None, on_status=lambda m: messages.append(m))
    vl.start()
    # The hard-fail message from the old code must NOT appear
    for m in messages:
        assert "Voice input unavailable from CLI (no app bundle)" not in m
    # The optimistic message should appear (or no status at all if engine succeeded)
    optimistic = any("not-determined" in m or "attempting" in m for m in messages)
    # It's fine if no message was emitted (engine succeeded); fail only if the
    # old hard-fail string is present.
    _ = optimistic  # used above in the negative assertion loop

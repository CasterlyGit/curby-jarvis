"""Headless unit tests for nlp_utils, stt new kwargs, and rule_table.fast_match.

All tests run with no microphone, no Speech framework, no display, and no
network.  Fake/stub objects are injected wherever native calls would occur.
"""
from __future__ import annotations

import pytest
from curby_jarvis import nlp_utils, stt
from curby_jarvis.rule_table import fast_match


# ===========================================================================
# nlp_utils.normalize_transcript
# ===========================================================================

class TestNormalizeTranscript:
    def test_empty_string_returns_empty(self):
        assert nlp_utils.normalize_transcript("") == ""

    def test_whitespace_only_returns_empty(self):
        assert nlp_utils.normalize_transcript("   ") == ""

    def test_no_fillers_passthrough(self):
        assert nlp_utils.normalize_transcript("open Spotify") == "open Spotify"

    def test_strips_leading_um(self):
        assert nlp_utils.normalize_transcript("um open Spotify") == "open Spotify"

    def test_strips_leading_uh(self):
        assert nlp_utils.normalize_transcript("uh close window") == "close window"

    def test_strips_leading_er(self):
        assert nlp_utils.normalize_transcript("er pause") == "pause"

    def test_strips_leading_like(self):
        assert nlp_utils.normalize_transcript("like next tab") == "next tab"

    def test_strips_leading_you_know(self):
        assert nlp_utils.normalize_transcript("you know mute") == "mute"

    def test_strips_multiple_leading_fillers(self):
        result = nlp_utils.normalize_transcript("um uh open Spotify")
        assert result == "open Spotify"

    def test_preserves_filler_mid_sentence(self):
        # 'like' in 'something like Spotify' — not stripped because it's not leading
        result = nlp_utils.normalize_transcript("play something like Spotify")
        assert result == "play something like Spotify"

    def test_collapses_internal_whitespace(self):
        result = nlp_utils.normalize_transcript("open   Spotify")
        assert result == "open Spotify"

    def test_drops_false_start_repeated_word(self):
        # "open open Spotify" → "open Spotify"
        result = nlp_utils.normalize_transcript("open open Spotify")
        assert result == "open Spotify"

    def test_does_not_drop_intentional_repetition_in_content(self):
        # "play play that funky music" — second + third word differ from first
        # after dedup only first duplicate pair is removed
        result = nlp_utils.normalize_transcript("play play that funky music")
        assert result == "play that funky music"

    def test_preserves_casing_of_content(self):
        result = nlp_utils.normalize_transcript("um open Finder")
        assert result == "open Finder"

    def test_strips_trailing_whitespace(self):
        result = nlp_utils.normalize_transcript("  pause  ")
        assert result == "pause"

    def test_filler_with_comma(self):
        # "um, open Spotify"
        result = nlp_utils.normalize_transcript("um, open Spotify")
        assert result == "open Spotify"

    def test_only_filler_returns_empty(self):
        result = nlp_utils.normalize_transcript("um uh")
        assert result == ""


# ===========================================================================
# nlp_utils.is_imperative_command
# ===========================================================================

class TestIsImperativeCommand:
    def test_open_is_imperative(self):
        assert nlp_utils.is_imperative_command("open Spotify") is True

    def test_pause_is_imperative(self):
        assert nlp_utils.is_imperative_command("pause") is True

    def test_next_tab_is_imperative(self):
        assert nlp_utils.is_imperative_command("next tab") is True

    def test_mute_is_imperative(self):
        assert nlp_utils.is_imperative_command("mute") is True

    def test_question_like_text_is_not_imperative(self):
        assert nlp_utils.is_imperative_command("what is the weather") is False

    def test_empty_is_not_imperative(self):
        assert nlp_utils.is_imperative_command("") is False

    def test_filler_only_is_not_imperative(self):
        assert nlp_utils.is_imperative_command("um uh") is False

    def test_very_long_text_is_not_imperative(self):
        long_text = "open " + "a" * 200
        assert nlp_utils.is_imperative_command(long_text) is False

    def test_with_leading_filler_still_detects_imperative(self):
        # normalize_transcript strips the filler first
        assert nlp_utils.is_imperative_command("um close window") is True


# ===========================================================================
# rule_table.fast_match
# ===========================================================================

class TestFastMatch:
    def test_pause_matches(self):
        assert fast_match("pause") is True

    def test_pause_music_matches(self):
        assert fast_match("pause the music") is True

    def test_next_tab_matches(self):
        assert fast_match("next tab") is True

    def test_mute_matches(self):
        assert fast_match("mute") is True

    def test_play_matches(self):
        assert fast_match("play") is True

    def test_close_window_matches(self):
        assert fast_match("close window") is True

    def test_partial_word_does_not_match(self):
        # "nex" — does not match any rule
        assert fast_match("nex") is False

    def test_empty_does_not_match(self):
        assert fast_match("") is False

    def test_unknown_verb_does_not_match(self):
        assert fast_match("xyzzy something") is False

    def test_deictic_click_does_not_match(self):
        # "click this" matches rule but is deictic (needs_pointer=True) → False
        assert fast_match("click this") is False

    def test_move_this_here_does_not_match(self):
        # deictic two-point move
        assert fast_match("move this to here") is False


# ===========================================================================
# stt.VoiceListener — new kwargs accepted and callbacks invoked on simulated
# partial WITHOUT a real mic.
# ===========================================================================

class TestVoiceListenerNewKwargs:
    """These tests drive VoiceListener purely through its Python-layer without
    starting the native audio engine.  We call the result handler directly
    (simulating what the Speech framework would do) and verify callbacks fire.
    """

    def _make_fake_result(self, text: str, is_final: bool = False):
        """Minimal fake SFSpeechRecognitionResult-like object."""
        class FakeTranscription:
            def formattedString(self):
                return text

        class FakeResult:
            def bestTranscription(self):
                return FakeTranscription()

            def isFinal(self):
                return is_final

        return FakeResult()

    def test_on_partial_kwarg_accepted(self):
        partials = []
        vl = stt.VoiceListener(
            on_utterance=lambda t: None,
            on_partial=partials.append,
        )
        assert vl.running is False  # no mic started

    def test_on_level_kwarg_accepted(self):
        levels = []
        vl = stt.VoiceListener(
            on_utterance=lambda t: None,
            on_level=levels.append,
        )
        assert vl.running is False

    def test_fast_endpoint_check_kwarg_accepted(self):
        vl = stt.VoiceListener(
            on_utterance=lambda t: None,
            fast_endpoint_check=lambda text: False,
        )
        assert vl.running is False

    def test_all_three_new_kwargs_together(self):
        vl = stt.VoiceListener(
            on_utterance=lambda t: None,
            on_partial=lambda t: None,
            on_level=lambda f: None,
            fast_endpoint_check=lambda t: False,
        )
        assert vl.running is False

    def test_on_partial_fires_when_result_handler_called(self):
        partials = []
        vl = stt.VoiceListener(
            on_utterance=lambda t: None,
            on_partial=partials.append,
        )
        # Simulate a partial result arriving from the Speech framework.
        fake_result = self._make_fake_result("pause the music", is_final=False)
        vl._on_result(fake_result, None)
        assert partials == ["pause the music"]

    def test_on_partial_fires_only_on_text_change(self):
        partials = []
        vl = stt.VoiceListener(
            on_utterance=lambda t: None,
            on_partial=partials.append,
        )
        fake_result = self._make_fake_result("pause")
        # First call: text changes from "" to "pause" → fires.
        vl._on_result(fake_result, None)
        assert len(partials) == 1
        # Second call: same text → no change → does NOT fire again.
        vl._on_result(fake_result, None)
        assert len(partials) == 1

    def test_on_partial_not_called_on_error(self):
        partials = []
        vl = stt.VoiceListener(
            on_utterance=lambda t: None,
            on_partial=partials.append,
        )
        # error passed → task_dead, on_partial never fires.
        vl._on_result(None, Exception("recognition error"))
        assert partials == []

    def test_on_partial_not_called_when_none(self):
        # Regression: on_partial=None (default) must never raise.
        vl = stt.VoiceListener(on_utterance=lambda t: None)
        fake_result = self._make_fake_result("mute")
        vl._on_result(fake_result, None)  # should not raise

    def test_fast_endpoint_check_stored_on_instance(self):
        checker = lambda t: True  # noqa: E731
        vl = stt.VoiceListener(
            on_utterance=lambda t: None,
            fast_endpoint_check=checker,
        )
        assert vl._fast_endpoint_check is checker

    def test_default_kwargs_are_none(self):
        vl = stt.VoiceListener(on_utterance=lambda t: None)
        assert vl._on_partial is None
        assert vl._on_level is None
        assert vl._fast_endpoint_check is None

    def test_existing_tests_unaffected_existing_constructor(self):
        """Existing callers that pass only on_utterance still construct fine."""
        vl = stt.VoiceListener(lambda _t: None)
        assert vl.running is False

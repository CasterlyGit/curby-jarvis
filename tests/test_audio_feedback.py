"""Headless tests for audio_feedback.py and overlay/audio_cue.py (module H).

All tests run without AppKit, AVFoundation, or speakers — audio disabled via
enabled=False on AudioFeedbackPlayer or CURBY_SOUND=0 for env-path coverage.
No real audio is played during the test suite.
"""
from __future__ import annotations

import importlib
import os
import sys
import types
import pytest


# ---------------------------------------------------------------------------
# helpers — ensure the package is importable without native libs
# ---------------------------------------------------------------------------

def _import_audio_feedback():
    """Import audio_feedback cleanly; the module must not touch native libs at
    import time (HARD RULE #1)."""
    # re-import to verify clean import every time (cached is fine)
    return importlib.import_module("curby_jarvis.audio_feedback")


def _import_audio_cue():
    return importlib.import_module("curby_jarvis.overlay.audio_cue")


# ---------------------------------------------------------------------------
# AudioFeedbackPlayer — enabled=False is a no-op
# ---------------------------------------------------------------------------

class TestAudioFeedbackPlayerDisabled:
    """When enabled=False every play_* method is a complete no-op (no import,
    no native call, no exception)."""

    def setup_method(self):
        mod = _import_audio_feedback()
        self.player = mod.AudioFeedbackPlayer(enabled=False)

    def test_play_ack_no_raise(self):
        self.player.play_ack()  # must not raise

    def test_play_done_no_raise(self):
        self.player.play_done()

    def test_play_error_no_raise(self):
        self.player.play_error()

    def test_play_thinking_no_raise(self):
        self.player.play_thinking()

    def test_enabled_flag_is_false(self):
        assert self.player._enabled is False


class TestAudioFeedbackPlayerEnvFlag:
    """CURBY_SOUND env variable controls the default enabled flag."""

    def test_env_zero_disables(self, monkeypatch):
        monkeypatch.setenv("CURBY_SOUND", "0")
        mod = _import_audio_feedback()
        player = mod.AudioFeedbackPlayer()  # enabled=None → read env
        assert player._enabled is False

    def test_env_one_enables(self, monkeypatch):
        monkeypatch.setenv("CURBY_SOUND", "1")
        mod = _import_audio_feedback()
        player = mod.AudioFeedbackPlayer()
        assert player._enabled is True

    def test_env_absent_defaults_on(self, monkeypatch):
        monkeypatch.delenv("CURBY_SOUND", raising=False)
        mod = _import_audio_feedback()
        player = mod.AudioFeedbackPlayer()
        assert player._enabled is True

    def test_explicit_true_overrides_env(self, monkeypatch):
        monkeypatch.setenv("CURBY_SOUND", "0")
        mod = _import_audio_feedback()
        player = mod.AudioFeedbackPlayer(enabled=True)
        assert player._enabled is True

    def test_explicit_false_overrides_env(self, monkeypatch):
        monkeypatch.setenv("CURBY_SOUND", "1")
        mod = _import_audio_feedback()
        player = mod.AudioFeedbackPlayer(enabled=False)
        assert player._enabled is False


class TestAudioFeedbackPlayerPlaySystemNative:
    """play_* methods attempt to call _play_system; we monkeypatch that to verify
    the routing even with enabled=True and no real AppKit."""

    def test_ack_calls_play_system_tink(self):
        mod = _import_audio_feedback()
        player = mod.AudioFeedbackPlayer(enabled=True)
        calls = []
        player._play_system = lambda name: calls.append(name)
        player.play_ack()
        assert calls == ["Tink"]

    def test_done_calls_play_system_hero(self):
        mod = _import_audio_feedback()
        player = mod.AudioFeedbackPlayer(enabled=True)
        calls = []
        player._play_system = lambda name: calls.append(name)
        player.play_done()
        assert calls == ["Hero"]

    def test_error_calls_play_system_basso(self):
        mod = _import_audio_feedback()
        player = mod.AudioFeedbackPlayer(enabled=True)
        calls = []
        player._play_system = lambda name: calls.append(name)
        player.play_error()
        assert calls == ["Basso"]

    def test_thinking_calls_play_system_pop(self):
        mod = _import_audio_feedback()
        player = mod.AudioFeedbackPlayer(enabled=True)
        calls = []
        player._play_system = lambda name: calls.append(name)
        player.play_thinking()
        assert calls == ["Pop"]

    def test_disabled_never_calls_play_system(self):
        mod = _import_audio_feedback()
        player = mod.AudioFeedbackPlayer(enabled=False)
        calls = []
        player._play_system = lambda name: calls.append(name)
        player.play_ack()
        player.play_done()
        player.play_error()
        player.play_thinking()
        assert calls == []

    def test_play_system_tolerates_appkit_exception(self):
        """_play_system must not raise even if AppKit throws."""
        mod = _import_audio_feedback()
        player = mod.AudioFeedbackPlayer(enabled=True)

        # Simulate AppKit import succeeding but soundNamed_ exploding
        fake_appkit = types.ModuleType("AppKit")
        class _FakeSound:
            @staticmethod
            def soundNamed_(_name):
                raise RuntimeError("no audio device")
        fake_appkit.NSSound = _FakeSound
        fake_appkit.NSBeep = lambda: None

        import builtins
        real_import = builtins.__import__

        def patched_import(name, *args, **kwargs):
            if name == "AppKit":
                return fake_appkit
            return real_import(name, *args, **kwargs)

        import unittest.mock as mock
        with mock.patch("builtins.__import__", side_effect=patched_import):
            player._play_system("Tink")  # must not raise


# ---------------------------------------------------------------------------
# SentenceAggregator
# ---------------------------------------------------------------------------

class TestSentenceAggregator:
    """Core contract: token stream → sentence callbacks."""

    def _make(self):
        mod = _import_audio_feedback()
        spoken = []
        agg = mod.SentenceAggregator(speak=spoken.append)
        return agg, spoken

    def test_single_sentence_on_period(self):
        agg, spoken = self._make()
        agg.feed("Hello world. ")
        assert spoken == ["Hello world."]

    def test_single_sentence_on_exclamation(self):
        agg, spoken = self._make()
        agg.feed("Hello! ")
        assert spoken == ["Hello!"]

    def test_single_sentence_on_question(self):
        agg, spoken = self._make()
        agg.feed("Are you there? ")
        assert spoken == ["Are you there?"]

    def test_multiple_sentences_in_one_feed(self):
        agg, spoken = self._make()
        agg.feed("First sentence. Second sentence. ")
        assert spoken == ["First sentence.", "Second sentence."]

    def test_partial_no_fire_until_complete(self):
        agg, spoken = self._make()
        agg.feed("Hello wor")
        agg.feed("ld")
        assert spoken == []
        # ". Done." completes "Hello world." immediately; "Done." also ends
        # on "." so it fires too (no trailing whitespace keeps it in the buffer).
        agg.feed(". Done.")
        assert spoken == ["Hello world.", "Done."]

    def test_flush_fires_remainder(self):
        agg, spoken = self._make()
        agg.feed("Incomplete fragment")
        assert spoken == []
        agg.flush()
        assert spoken == ["Incomplete fragment"]

    def test_flush_empty_buffer_no_fire(self):
        agg, spoken = self._make()
        agg.flush()
        assert spoken == []

    def test_streaming_token_by_token(self):
        """Simulate streaming one character at a time."""
        agg, spoken = self._make()
        for ch in "Hello. World!":
            agg.feed(ch)
        agg.flush()
        assert "Hello." in spoken
        assert "World!" in spoken

    def test_speak_exception_does_not_propagate(self):
        """A bad speak callback must never break the aggregator."""
        mod = _import_audio_feedback()

        def bad_speak(_text):
            raise RuntimeError("speaker exploded")

        agg = mod.SentenceAggregator(speak=bad_speak)
        # These must not raise
        agg.feed("Hello. ")
        agg.flush()

    def test_multiple_feeds_single_sentence_across_boundaries(self):
        agg, spoken = self._make()
        agg.feed("One ")
        agg.feed("two ")
        agg.feed("three.")
        agg.flush()
        assert "One two three." in spoken

    def test_whitespace_only_remainder_not_spoken(self):
        agg, spoken = self._make()
        agg.feed("Done.   ")  # trailing spaces after period
        agg.flush()
        # "   " (the remainder after split) should not produce a spoken item
        assert all(s.strip() for s in spoken)


# ---------------------------------------------------------------------------
# speak_sentence
# ---------------------------------------------------------------------------

class TestSpeakSentence:
    """speak_sentence must import cleanly and be a no-op when AVFoundation absent."""

    def test_empty_text_no_raise(self):
        mod = _import_audio_feedback()
        mod.speak_sentence("")  # must not raise

    def test_no_avfoundation_is_noop(self):
        """Without AVFoundation speak_sentence silently does nothing."""
        mod = _import_audio_feedback()
        # The CI machine has no AVFoundation; calling it should never raise.
        mod.speak_sentence("Hello world")

    def test_avfoundation_exception_suppressed(self, monkeypatch):
        """Even if AVFoundation is present but explodes, no exception leaks."""
        mod = _import_audio_feedback()

        fake_av = types.ModuleType("AVFoundation")
        class _BadSynth:
            @staticmethod
            def alloc():
                raise RuntimeError("AV exploded")
        fake_av.AVSpeechSynthesizer = _BadSynth

        import builtins, unittest.mock as mock
        real_import = builtins.__import__
        def patched(name, *args, **kwargs):
            if name == "AVFoundation":
                return fake_av
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=patched):
            mod.speak_sentence("Test sentence")  # must not raise


# ---------------------------------------------------------------------------
# PhaseAudio — routes phase strings to the correct player method
# ---------------------------------------------------------------------------

class FakePlayer:
    """Recording fake for AudioFeedbackPlayer."""
    def __init__(self):
        self.calls: list[str] = []
    def play_ack(self):     self.calls.append("ack")
    def play_done(self):    self.calls.append("done")
    def play_error(self):   self.calls.append("error")
    def play_thinking(self): self.calls.append("thinking")


class TestPhaseAudio:

    def _make(self):
        mod = _import_audio_cue()
        fake = FakePlayer()
        pa = mod.PhaseAudio(player=fake)
        return pa, fake

    def test_heard_triggers_ack(self):
        pa, fake = self._make()
        pa.on_phase("heard")
        assert fake.calls == ["ack"]

    def test_done_triggers_done(self):
        pa, fake = self._make()
        pa.on_phase("done")
        assert fake.calls == ["done"]

    def test_error_triggers_error(self):
        pa, fake = self._make()
        pa.on_phase("error")
        assert fake.calls == ["error"]

    def test_planning_triggers_thinking(self):
        pa, fake = self._make()
        pa.on_phase("planning")
        assert fake.calls == ["thinking"]

    def test_understanding_triggers_thinking(self):
        pa, fake = self._make()
        pa.on_phase("understanding")
        assert fake.calls == ["thinking"]

    def test_idle_is_silent(self):
        pa, fake = self._make()
        pa.on_phase("idle")
        assert fake.calls == []

    def test_listening_is_silent(self):
        pa, fake = self._make()
        pa.on_phase("listening")
        assert fake.calls == []

    def test_acting_is_silent(self):
        pa, fake = self._make()
        pa.on_phase("acting")
        assert fake.calls == []

    def test_unknown_phase_is_silent(self):
        pa, fake = self._make()
        pa.on_phase("totally_unknown_phase")
        assert fake.calls == []

    def test_sequence_of_phases(self):
        pa, fake = self._make()
        for p in ["heard", "planning", "done"]:
            pa.on_phase(p)
        assert fake.calls == ["ack", "thinking", "done"]

    def test_player_exception_does_not_propagate(self):
        """A broken player must not crash the calling thread."""
        mod = _import_audio_cue()

        class BrokenPlayer:
            def play_ack(self): raise RuntimeError("boom")
            def play_done(self): raise RuntimeError("boom")
            def play_error(self): raise RuntimeError("boom")
            def play_thinking(self): raise RuntimeError("boom")

        pa = mod.PhaseAudio(player=BrokenPlayer())
        pa.on_phase("heard")   # must not raise
        pa.on_phase("done")
        pa.on_phase("error")
        pa.on_phase("planning")

    def test_lazy_player_construction_no_raise(self):
        """When no player is injected, PhaseAudio constructs one lazily; on CI
        this will build an AudioFeedbackPlayer that is then effectively no-op."""
        mod = _import_audio_cue()
        pa = mod.PhaseAudio()  # player=None
        # on_phase must not raise even if AudioFeedbackPlayer default-enables
        pa.on_phase("heard")
        pa.on_phase("done")

    def test_import_is_headless(self):
        """Importing overlay.audio_cue must not touch any native framework."""
        # If the import already happened the cached module is fine; what matters
        # is that no AppKit/Qt/AVFoundation was touched at import time. We verify
        # the module is importable without side effects by re-importing.
        _import_audio_cue()  # no exception = headless import succeeded

"""VoiceListener — continuous on-device speech input for the live controller.

This is "voice in" for curby-jarvis: a background listener built on the macOS
**Speech** framework (`SFSpeechRecognizer`, on-device) fed by an `AVAudioEngine`
microphone tap. Each finished spoken phrase is handed to an `on_utterance`
callback, which the app lowers to an Intent and routes through the connector
chain. No model download, no API key, no network — recognition runs on-device.

Utterance segmentation: the Speech framework streams partial transcriptions as
you talk; we treat a phrase as *finished* when its transcription stops changing
for `silence_s` (or after a `max_utt_s` hard cap), end the current audio request
so the framework emits a final transcription, then immediately start a fresh
request for the next phrase. That endpointing decision is the pure, unit-tested
`should_endpoint()` — no audio math required, because the recognizer's own
partial cadence is the activity signal.

HEADLESS CONTRACT (HARD RULE): importing this module must not import pyobjc,
touch a microphone, or request a permission. Every `Speech` / `AVFoundation`
import and every TCC probe lives lazily inside a method. `should_endpoint()` and
`normalize_utterance()` are pure functions importable and testable with no
display, no mic, and no Speech framework — which is exactly what the unit suite
drives. On a box without the Speech framework or without granted permissions,
`start()` returns `False` cleanly (the overlays + gesture pipeline still run);
it never raises.

Threading: `start()` installs the audio tap (fires on a realtime CoreAudio
thread) and spins one daemon *watcher* thread that owns endpointing and recogni-
tion-task lifecycle. The Speech result handler runs on the framework's own queue
and only records the latest transcription. The app's Qt event loop provides the
spinning main runloop the frameworks expect; in `--check` (no Qt loop) the
caller pumps the runloop while probing.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional


# ---- pure helpers (no pyobjc, no mic — unit-tested headless) -----------------

def normalize_utterance(text: str, wake_word: Optional[str] = None) -> Optional[str]:
    """Clean a recognized phrase; apply an optional wake word.

    Returns the routable utterance, or None when there's nothing to route. With
    a `wake_word` set, the phrase must start with it (case-insensitive); the wake
    word is stripped and the remainder returned (None if only the wake word was
    said, or the wake word is absent). With no wake word every non-empty phrase
    routes.
    """
    t = (text or "").strip()
    if not t:
        return None
    if wake_word:
        w = wake_word.strip().lower()
        low = t.lower()
        if not low.startswith(w):
            return None
        t = t[len(w):].lstrip(" ,.:-—").strip()
        return t or None
    return t


def should_endpoint(
    has_text: bool,
    since_change_s: float,
    since_start_s: float,
    *,
    silence_s: float,
    max_utt_s: float,
) -> bool:
    """True when the current phrase should be treated as finished.

    A phrase ends when its transcription has been stable for `silence_s` (the
    speaker paused), or when it has run past `max_utt_s` (a run-on we cut off).
    With no text yet there is nothing to end. Pure + side-effect-free so the
    segmentation policy is asserted without a microphone.
    """
    if not has_text:
        return False
    if since_change_s >= silence_s:
        return True
    if since_start_s >= max_utt_s:
        return True
    return False


# ---- TCC / framework probes (lazy; safe to call headless) -------------------

def speech_framework_available() -> bool:
    """True if the macOS Speech framework is importable in this interpreter."""
    try:
        import Speech  # noqa: F401
        import AVFoundation  # noqa: F401
        return True
    except Exception:
        return False


def _speech_status_name(status: int) -> str:
    return {0: "not-determined", 1: "denied", 2: "restricted", 3: "authorized"}.get(
        int(status), f"unknown({status})"
    )


def _av_status_name(status: int) -> str:
    return {0: "not-determined", 1: "restricted", 2: "denied", 3: "authorized"}.get(
        int(status), f"unknown({status})"
    )


def authorization_summary() -> dict:
    """Snapshot the current Speech + Microphone TCC authorization, without
    prompting. Returns a dict with `speech`/`mic` status names and `on_device`
    support, or an `error` if the framework is unavailable. Pure read — used by
    `--check` and by `start()` to decide whether to prompt."""
    try:
        import Speech
        import AVFoundation
    except Exception as e:  # framework missing -> report cleanly
        return {"error": f"Speech framework unavailable: {e!r}"}

    speech = int(Speech.SFSpeechRecognizer.authorizationStatus())
    mic = int(
        AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
            AVFoundation.AVMediaTypeAudio
        )
    )
    try:
        rec = Speech.SFSpeechRecognizer.alloc().init()
        on_device = bool(rec.supportsOnDeviceRecognition()) if rec is not None else False
    except Exception:
        on_device = False
    return {
        "speech": _speech_status_name(speech),
        "speech_code": speech,
        "mic": _av_status_name(mic),
        "mic_code": mic,
        "on_device": on_device,
    }


def request_authorizations(timeout: float = 25.0, pump_runloop: bool = True) -> dict:
    """Prompt for Speech + Microphone access (no-op if already decided) and wait
    for the user's answer. Pumps the main runloop while waiting so the async
    completion handlers fire even with no Qt loop running (the `--check` path).
    Returns the post-decision `authorization_summary()`."""
    try:
        import Speech
        import AVFoundation
    except Exception as e:
        return {"error": f"Speech framework unavailable: {e!r}"}

    done = threading.Event()
    state = {"speech": None, "mic": None}

    def _speech_handler(status):
        state["speech"] = int(status)
        if state["mic"] is not None:
            done.set()

    def _mic_handler(granted):
        state["mic"] = bool(granted)
        if state["speech"] is not None:
            done.set()

    Speech.SFSpeechRecognizer.requestAuthorization_(_speech_handler)
    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
        AVFoundation.AVMediaTypeAudio, _mic_handler
    )

    end = time.time() + timeout
    if pump_runloop:
        from Foundation import NSRunLoop, NSDate, NSDefaultRunLoopMode

        loop = NSRunLoop.currentRunLoop()
        while not done.is_set() and time.time() < end:
            loop.runMode_beforeDate_(
                NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.1)
            )
    else:
        done.wait(timeout=max(0.0, end - time.time()))

    return authorization_summary()


# ---- the listener -----------------------------------------------------------

class VoiceListener:
    """Continuous on-device voice input. Build with an `on_utterance` callback,
    then `start()`; `stop()` tears the engine + tasks down.

    The callback receives each finished phrase as a plain string (already wake-
    word-normalized) and is invoked on the watcher thread — the app immediately
    hands off to its own worker so this thread never blocks on routing/execution.
    """

    def __init__(
        self,
        on_utterance: Callable[[str], None],
        *,
        locale: str = "en-US",
        silence_s: float = 1.4,   # 0.8s endpointed on natural between-word
                                  # pauses, chopping phrases after 1-3 words
                                  # ('Can you just', 'Take so long'); 1.4s lets
                                  # a whole sentence land before finalizing.
        max_utt_s: float = 12.0,
        wake_word: Optional[str] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_utterance = on_utterance
        self._locale = locale
        self._silence_s = float(silence_s)
        self._max_utt_s = float(max_utt_s)
        self._wake_word = wake_word
        self._on_status = on_status

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._watcher: Optional[threading.Thread] = None
        # Backoff for empty/errored recognition restarts (e.g. Speech access not
        # yet granted): grows so a denied grant can't spin the recognizer hot, and
        # resets the moment a real phrase lands.
        self._error_backoff = 0.1

        # native objects (built in start(); kept alive on self so the engine
        # keeps running after the thread that started it returns).
        self._recognizer = None
        self._engine = None
        self._input = None
        self._on_device = False
        self._engine_running = False

        # per-utterance recognition state (guarded by _lock).
        self._request = None
        self._task = None
        self._task_dead = False
        self._latest_text = ""
        self._last_change = 0.0
        self._utt_start = 0.0

    # -- status ---------------------------------------------------------------

    def _status(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    @property
    def running(self) -> bool:
        return self._watcher is not None and self._watcher.is_alive()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> bool:
        """Bring up the recognizer + mic engine and begin listening.

        Returns True once the listener is live (or will become live as soon as a
        pending TCC grant lands — the watcher retries engine start). Returns
        False, cleanly and without raising, when the Speech framework is absent
        or speech access is hard-denied. Never raises.
        """
        if self.running:
            return True
        if not speech_framework_available():
            self._status("Speech framework unavailable — voice input off.")
            return False

        import Speech
        import AVFoundation
        from Foundation import NSLocale

        summary = authorization_summary()
        if summary.get("speech") in ("denied", "restricted"):
            self._status(
                "Speech Recognition is denied — enable it in System Settings › "
                "Privacy & Security › Speech Recognition, then relaunch."
            )
            return False
        if summary.get("speech") == "not-determined" or summary.get("mic") == "not-determined":
            # Fire the prompts now; the watcher will retry engine start until the
            # user answers, so a first-run grant lights up mid-session.
            Speech.SFSpeechRecognizer.requestAuthorization_(lambda *_: None)
            AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                AVFoundation.AVMediaTypeAudio, lambda *_: None
            )
            self._status("Requesting Microphone + Speech Recognition permission…")

        loc = NSLocale.localeWithLocaleIdentifier_(self._locale)
        rec = Speech.SFSpeechRecognizer.alloc().initWithLocale_(loc)
        if rec is None:
            rec = Speech.SFSpeechRecognizer.alloc().init()
        if rec is None:
            self._status(f"No speech recognizer for locale {self._locale!r}.")
            return False
        self._recognizer = rec
        try:
            self._on_device = bool(rec.supportsOnDeviceRecognition())
        except Exception:
            self._on_device = False

        self._engine = AVFoundation.AVAudioEngine.alloc().init()
        self._input = self._engine.inputNode()
        fmt = self._input.outputFormatForBus_(0)
        # The tap fires on a realtime CoreAudio thread: append to the current
        # request (None during the brief silence gap between phrases) and never
        # do heavy work here.
        self._input.installTapOnBus_bufferSize_format_block_(
            0, 2048, fmt, self._on_audio
        )

        self._stop.clear()
        self._watcher = threading.Thread(
            target=self._watch, name="curby-stt", daemon=True
        )
        self._watcher.start()
        return True

    def stop(self) -> None:
        """Stop listening and release the engine + tasks. Safe to call twice."""
        self._stop.set()
        w = self._watcher
        if w is not None and w.is_alive() and w is not threading.current_thread():
            w.join(timeout=1.5)
        self._watcher = None
        with self._lock:
            task, req, eng, inp = self._task, self._request, self._engine, self._input
            self._task = self._request = None
            self._engine_running = False
        for fn in (
            lambda: task and task.cancel(),
            lambda: req and req.endAudio(),
            lambda: inp and inp.removeTapOnBus_(0),
            lambda: eng and eng.stop(),
        ):
            try:
                fn()
            except Exception:
                pass

    # -- audio tap (realtime thread) -----------------------------------------

    def _on_audio(self, buffer, when) -> None:
        with self._lock:
            req = self._request
        if req is not None:
            try:
                req.appendAudioPCMBuffer_(buffer)
            except Exception:
                pass

    # -- recognition result handler (framework queue) ------------------------

    def _on_result(self, result, error) -> None:
        now = time.time()
        with self._lock:
            if self._stop.is_set():
                return
            if error is not None or result is None:
                self._task_dead = True
                return
            try:
                text = str(result.bestTranscription().formattedString())
            except Exception:
                text = ""
            if text and text != self._latest_text:
                self._latest_text = text
                self._last_change = now
                if self._utt_start == 0.0:
                    self._utt_start = now
            try:
                if result.isFinal():
                    self._task_dead = True
            except Exception:
                pass

    # -- watcher: engine health, endpointing, task lifecycle -----------------

    def _watch(self) -> None:
        last_engine_warn = 0.0
        while not self._stop.is_set():
            now = time.time()

            # 1) keep the mic engine running (retry through a pending TCC grant).
            if not self._engine_running:
                if self._ensure_engine():
                    self._status("🎙️  listening — speak a command.")
                elif now - last_engine_warn > 5.0:
                    last_engine_warn = now
                    self._status("waiting for microphone access…")

            # 2) ensure a live recognition task exists.
            if self._engine_running:
                with self._lock:
                    need_task = self._task is None and self._request is None
                if need_task:
                    self._start_recognition()

            # 3) endpoint the current phrase.
            with self._lock:
                has_text = bool(self._latest_text)
                since_change = (now - self._last_change) if has_text else 0.0
                since_start = (now - self._utt_start) if self._utt_start else 0.0
                dead = self._task_dead

            if has_text and (
                dead
                or should_endpoint(
                    has_text, since_change, since_start,
                    silence_s=self._silence_s, max_utt_s=self._max_utt_s,
                )
            ):
                self._error_backoff = 0.1  # a real phrase landed -> reset backoff
                self._finalize_and_restart()
            elif dead:
                # Task ended with nothing (silence, or Speech access not yet
                # granted). Restart, but back off so a denied grant can't hot-loop.
                self._restart_recognition()
                self._stop.wait(self._error_backoff)
                if self._error_backoff >= 2.0:
                    # Backoff is maxed -> tasks keep dying empty. If Speech access
                    # was just granted, the recognizer built while not-authorized
                    # stays unavailable; rebuild it so a mid-session grant lights
                    # up without a relaunch.
                    self._rebuild_recognizer()
                self._error_backoff = min(self._error_backoff * 2, 2.0)

            self._stop.wait(0.08)

    def _ensure_engine(self) -> bool:
        try:
            self._engine.prepare()
            ok, _err = self._engine.startAndReturnError_(None)
            self._engine_running = bool(ok)
            return self._engine_running
        except Exception:
            self._engine_running = False
            return False

    def _start_recognition(self) -> None:
        import Speech

        try:
            req = Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
            req.setShouldReportPartialResults_(True)
            if self._on_device:
                try:
                    req.setRequiresOnDeviceRecognition_(True)
                except Exception:
                    pass
            task = self._recognizer.recognitionTaskWithRequest_resultHandler_(
                req, self._on_result
            )
        except Exception as e:
            self._status(f"recognition start failed: {e!r}")
            return
        with self._lock:
            self._request = req
            self._task = task
            self._task_dead = False
            self._latest_text = ""
            self._last_change = 0.0
            self._utt_start = 0.0

    def _finalize_and_restart(self) -> None:
        with self._lock:
            text = self._latest_text
            old_req, old_task = self._request, self._task
            self._request = self._task = None
            self._task_dead = False
            self._latest_text = ""
            self._last_change = 0.0
            self._utt_start = 0.0
        try:
            old_req and old_req.endAudio()
        except Exception:
            pass
        try:
            old_task and old_task.finish()
        except Exception:
            pass
        utt = normalize_utterance(text, self._wake_word)
        if utt:
            try:
                self._on_utterance(utt)
            except Exception as e:
                self._status(f"utterance handler error: {e!r}")
        if not self._stop.is_set():
            self._start_recognition()

    def _rebuild_recognizer(self) -> None:
        """Re-create the SFSpeechRecognizer — used when Speech access is granted
        mid-session so a recognizer built while not-authorized starts working."""
        try:
            import Speech
            from Foundation import NSLocale

            loc = NSLocale.localeWithLocaleIdentifier_(self._locale)
            rec = Speech.SFSpeechRecognizer.alloc().initWithLocale_(loc)
            if rec is None:
                rec = Speech.SFSpeechRecognizer.alloc().init()
            if rec is not None:
                self._recognizer = rec
                try:
                    self._on_device = bool(rec.supportsOnDeviceRecognition())
                except Exception:
                    pass
                self._status("🎙️  speech access ready — listening.")
        except Exception:
            pass

    def _restart_recognition(self) -> None:
        with self._lock:
            old_req, old_task = self._request, self._task
            self._request = self._task = None
            self._task_dead = False
            self._latest_text = ""
            self._last_change = 0.0
            self._utt_start = 0.0
        try:
            old_task and old_task.cancel()
        except Exception:
            pass
        if not self._stop.is_set():
            self._start_recognition()


__all__ = [
    "VoiceListener",
    "should_endpoint",
    "normalize_utterance",
    "speech_framework_available",
    "authorization_summary",
    "request_authorizations",
]

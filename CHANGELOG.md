# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-05-31

### Added

- **On-device voice input** (`stt.py`): a continuous `VoiceListener` built on the macOS Speech framework (`SFSpeechRecognizer`, on-device) fed by an `AVAudioEngine` microphone tap — no API key, no network, no model download. Phrases are segmented by partial-transcription-stability endpointing (`should_endpoint`) with a hard run-on cap, then handed to the router. Optional wake word via `CURBY_WAKE`.
- **Full `--live` controller**: brings up the Qt overlays, drives the crosshair reticle from the live gesture pointer at 30 fps, listens on-device, lowers each phrase, binds deixis to the fused pointer, and executes the cheapest confident connector — all with the audio/UI path on the Qt main thread and routing/execution on worker threads so input always snaps.
- **Confirm gate, wired**: `_overlay_confirm` now shows the frosted card and blocks the dispatch worker on a real Confirm/Cancel (marshaled to the Qt main thread), serialized so two phrases can't race one card, and defaulting to Cancel on a 30 s timeout — an irreversible action never fires without an explicit human Confirm.
- **`--check` preflight**: probes and requests Microphone + Speech Recognition, reports Accessibility trust, the `claude` agent floor, and gesture-websocket reachability, and opens the relevant Privacy & Security pane for anything missing.
- Accessory activation policy for the overlay app (no Dock icon / focus theft) via `macwin.set_accessory_policy`.

### Fixed

- `macwin.make_always_visible` no longer dereferences a non-backed `winId()` under the `offscreen`/`minimal` Qt platforms (CI), which could segfault the headless suite; it now no-ops cleanly when there is no window server.

### Tests

- 272 passing (1 skipped), fully headless — adds STT endpointing/normalization units and the live dispatch/confirm/reticle wiring driven with fakes.

[1.0.0]: https://github.com/CasterlyGit/curby-jarvis/releases/tag/v1.0.0

## [0.1.0] - 2026-05-31

### Added

- Hybrid CapabilityRouter: a cost-ranked chain of pluggable connectors where the cheapest connector that is confident and available wins, and any failure falls through gracefully to the next.
- The 7 connectors: `app_launch` (cost 1, NSWorkspace + URL-scheme open, no TCC needed), `media_key` (cost 2, auxiliary HID media keys), `menubar_ax` (cost 3, drives the focused app's menu bar via Accessibility), `deixis_click` (cost 4, AX press at the fused pointer), `browser_tab` (cost 6, browser tab control via warm osascript), the LLM intent-parse seam (cost 8), and `agent_fallback` (cost 10, open-ended last resort shelling to `claude -p`).
- Point-and-say deixis pipeline: hand-signal gesture websocket -> pointer fusion (aimed/fresh/confident samples) -> calibration (screen mapping) -> `deixis_click` AX press at the fused pointer.
- JARVIS HUD overlay: a crosshair reticle at the pointer plus a preview card showing the chosen action, mechanism, and risk before it runs, with a confirm prompt on irreversible actions.
- Safety layer: every AX call is watchdog-wrapped (timeout falls through, never hangs), Secure-Input detection blocks keystroke injection when a password field is focused, and connectors never raise (errors return in a ConnectorResult so routing degrades gracefully).
- Headless `--dry-run` audit JSON (chosen connector, mechanism tag, risk, must_confirm, zero side effects) plus the 8-demo golden CLI harness that drives all point-and-say demos through the real CLI as subprocesses.
- Test suite: 248 passing (1 skipped), fully headless.

### Known limitations

- Live-machine TCC (macOS Accessibility permission) integration is pending.
- The LLM intent-parse and `agent_fallback` tiers require API / CLI availability.

[0.1.0]: https://github.com/CasterlyGit/curby-jarvis/releases/tag/v0.1.0

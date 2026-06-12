# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-05-31

### Added

- **AgentLoopConnector (cost 9) — the open-ended path is now structured**: instead of a black-box `claude -p` subprocess, open-ended utterances enter a Claude tool-use loop that exposes every connector as a named tool and composes multi-step plans, dispatching each tool call back through the router. The system can now do *anything* via voice and gesture — not a closed verb set.
- **MCP bridge (cost 7)**: any MCP server listed in `~/.curby/mcp_servers.json` is surfaced dynamically as a router tool, giving curby-jarvis first-class access to any MCP-compatible service without new connectors.
- **ComputerUseConnector (cost 11)**: pixel-level vision fallback for UIs that are opaque to Accessibility — screen-capture + vision model drives pointer and keyboard as a last resort. Cost ladder now runs 1 → 2 → 3 → 4 → 6 → 7 → 8 → 9 → 10 → 11; fast deterministic rungs are untouched.
- **JARVIS HUD — SessionPhase state machine (UI-01..15)**: a single `idle → listening → heard → understanding → planning → acting → done/error` state machine drives all surfaces in unison — animated reticle ORB (audio-reactive / thinking-spin / lock-on bracket), live partial-transcript caption under the crosshair, ambient screen-edge glow, Frosted Console card with mechanism + latency chip + undo toast, radial connector-chain diagnostic ring, command/undo history overlay, adaptive-ink legibility, and on-device sound cues (sub-100 ms ack chime + sentence-stream TTS). New modules: `overlay/caption.py`, `edge_glow.py`, `diag_ring.py`, `history_overlay.py`, `session_phase.py`.
- **Iron-Man gesture bus** (`pointer/gesture_bus.py`): the hand-signal pointer stream feeds a hysteresis-gated, cooldown-protected event bus — pinch = confirm pending action; open palm = STOP / barge-in (cancels any in-flight action); swipe = directional verb (next / prev / scroll).
- **Latency model — speculative parse + sentence-stream TTS + barge-in**: streaming STT partials feed a speculative rule-parse off the Speech queue so the router starts before the utterance ends; sentence-stream TTS plays the first sentence before the full response is ready; per-stage latency is tracked with rolling P95 SLO budgets; barge-in (new utterance or open-palm STOP) cancels in-flight actions immediately.
- **Robustness and observability layer (INF-01..15)**: per-utterance `trace_id` telemetry written as JSONL in OpenTelemetry GenAI-aligned format (`telemetry.py`); per-connector circuit breakers that skip repeatedly-failing connectors and degrade to offline mode (`circuit_breaker.py`); `--check` preflight extended to cover Microphone, Speech, Accessibility, Automation, Screen Recording, agent availability, and MCP reachability; session memory + undo ledger in SQLite (`undo_ledger.py`); one-time execution-grant token vault (sensitive agent actions cannot be silently replayed); multi-step task engine with per-step confirm.

### Changed

- Open-ended utterances now route to `agent_loop` (cost 9 — structured Claude tool-use loop) instead of `agent_fallback` (cost 10 — raw `claude -p`). `agent_fallback` remains as a last resort below `agent_loop`.
- Cost ladder extended: MCP bridge inserted at cost 7, `agent_loop` at cost 9, `computer_use` at cost 11 — 8 live connectors plus MCP adapters registered dynamically.

### Tests

- 742 passing, 4 skipped (was 272 / 1); 49 modules, ~11,500 src LOC (was ~7,589). Adversarially reviewed: 21 findings (10 high / 6 med / 5 low) — all fixed.

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

[2.0.0]: https://github.com/CasterlyGit/curby-jarvis/releases/tag/v2.0.0
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

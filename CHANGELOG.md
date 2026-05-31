# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

# curby-jarvis — CLAUDE.md

## 1. One-line pitch + status

Voice + hand-gesture universal macOS controller — point at a target, say what to do, it routes to the cheapest capable connector and does it.

**Status:** v2.0.0 "JARVIS revamp" — branch `revamp/jarvis-v2` (off main, unmerged). 742 tests green, 4 skipped. 49 modules, ~11,500 src LOC.

---

## 2. Run / test

```bash
# Tests (offscreen Qt — required for headless)
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q

# App
python -m curby_jarvis.app --check          # preflight: mic / Speech / AX / Screen-Recording / MCP / agent
python -m curby_jarvis.app --live           # on-device voice controller (streaming STT → route → confirm → execute)
python -m curby_jarvis.app --say "open Spotify"                          # route + execute
python -m curby_jarvis.app --say "open Spotify" --dry-run                # audit JSON, zero side effects
python -m curby_jarvis.app --say "move that there" --pointer 100,200 --pointer2 800,600 --dry-run
```

Venv: `.venv/`. Python 3.11+. Optional vision extra: `pip install -e ".[vision]"` for `computer_use`.

---

## 3. Architecture / key patterns

### Hybrid CapabilityRouter

Connector chain ordered by `(cost, -confidence)`. **Cheapest confident + available connector wins.** If a connector fails it returns a `ConnectorResult` (never raises) and the router falls through to the next rung — graceful degradation, no crash.

### Full cost ladder

| Cost | Connector | Mechanism | TCC |
|:----:|-----------|-----------|-----|
| 1 | `app_launch` | NSWorkspace + URL-scheme open | none |
| 2 | `media_key` | Auxiliary HID media keys | none |
| 3 | `menubar_ax` | Focused app's menu bar via AX | Accessibility |
| 4 | `deixis_click` / `ax_press` | AX press/drag at the fused pointer | Accessibility |
| 6 | `browser_tab` / `browser_osascript` | Warm osascript tab control | Automation |
| 7 | `mcp` | MCP bridge — any server in `~/.curby/mcp_servers.json` becomes a router tool | varies |
| 8 | `intent_parse` | LLM intent-parse seam | — |
| 9 | `agent_loop` | Structured Claude tool-use loop: exposes every connector as a tool, composes multi-step plans, dispatches each tool call back through the router | — |
| 10 | `agent_fallback` | Unstructured last resort — shells to `claude -p` | — |
| 11 | `computer_use` | Pixel-level vision fallback for AX-opaque UIs | Screen Recording |

### Connector contract (`connectors/__init__.py`)

```python
can_handle(intent) -> float      # 0.0 = no, 0..1 = confidence
is_available(intent) -> bool     # cheap correctness probe (permissions, Secure Input, etc.)
preview(intent) -> PreviewCard   # build overlay card — NO side effects
execute(intent) -> ConnectorResult  # NEVER raises; errors in ConnectorResult
tool_schema() -> dict            # Anthropic tool definition for agent_loop (INF-02)
```

Each connector has a `.name` (mechanism tag for telemetry) and `.cost`. Network-bound connectors set `use_breaker = True` to get a lazy `CircuitBreaker`.

### Generalization thesis — `agent_loop` (cost 9)

The open-ended path is no longer a black-box subprocess. `AgentLoopConnector` runs a Claude tool-use loop that exposes every registered connector as a tool (`tool_schema()`), composes a multi-step plan, and dispatches each tool-use block back through the router via `intent_from_tool_input()`. Below it: MCP bridge (cost 7, dynamic) and `computer_use` (cost 11, pixel fallback). The deterministic fast path is untouched.

### SessionPhase state machine

`session_phase.py` defines: `idle → listening → heard → understanding → planning → acting → done/error`. ALL HUD surfaces (orb reticle, caption, edge glow, frosted console card, diag ring, history overlay) subscribe to this machine and update in unison.

### Infra: telemetry / circuit-breaker / undo-ledger / execution-grant

- **telemetry.py** — per-utterance `trace_id`, JSONL log, OTel-GenAI-aligned.
- **circuit_breaker.py** — per-connector breaker; tripped connector is skipped; degraded routing continues.
- **undo_ledger.py** — SQLite-backed undo log + session memory; console card surfaces undo toast.
- One-time execution-grant token vault — sensitive agent actions require a grant and can't be silently replayed.
- **Barge-in** — new utterance or open-palm STOP gesture cancels any in-flight action.

### Latency model

Streaming STT partials feed speculative rule-parse off the Speech queue — router starts before utterance ends. Cheap rungs (media, launch, tab) fire nearly immediately. Sentence-stream TTS plays the first sentence before the full response is ready. Per-stage P95 SLO budgets tracked.

### Iron-Man gestures (`pointer/gesture_bus.py`)

Gesture event bus with hysteresis + cooldown over the hand-signal pointer stream:
- pinch → confirm pending action
- open palm → STOP / barge-in (cancels in-flight)
- swipe → directional verb (next/prev/scroll)

---

## 4. Key files

```
src/curby_jarvis/
├── app.py                  CLI entrypoint + --live controller + --check preflight
├── router.py               Hybrid CapabilityRouter (cost, -confidence) chain
├── rule_table.py           golden lowering: utterance → verb/intent
├── intent.py               Intent model, PreviewCard, ConnectorResult, LLM parse seam
├── session_phase.py        SessionPhase state machine (all HUD surfaces subscribe here)
├── telemetry.py            per-utterance trace_id + JSONL/OTel-GenAI-aligned logging
├── undo_ledger.py          SQLite undo log + session memory
├── circuit_breaker.py      per-connector circuit breakers + degraded-mode routing
├── connectors/
│   ├── __init__.py         Connector base class + contract + intent_from_tool_input
│   ├── app_launch.py       cost 1  — NSWorkspace + URL-scheme (zero TCC)
│   ├── media_transport.py  cost 2  — HID media keys
│   ├── menu_command.py     cost 3  — focused app menu bar via AX
│   ├── deixis_click.py     cost 4  — AX press/drag at fused pointer
│   ├── browser_tab.py      cost 6  — tab control via warm osascript
│   ├── mcp_connector.py    cost 7  — MCP bridge (~/.curby/mcp_servers.json → tool)
│   ├── intent_parse.py     cost 8  — LLM intent parse
│   ├── agent_loop.py       cost 9  — Claude tool-use loop (multi-step planner)
│   ├── agent_fallback.py   cost 10 — last resort: claude -p subprocess
│   └── computer_use.py     cost 11 — pixel-level vision fallback (Screen Recording)
├── pointer/
│   ├── ws_client.py        hand-signal gesture websocket consumer
│   ├── fusion.py           fuses aimed/fresh/confident samples into pointer
│   ├── gesture_bus.py      gesture event bus: pinch/open-palm/swipe + hysteresis
│   └── calibration.py      screen coordinate mapping
├── overlay/
│   ├── reticle.py          animated ORB reticle (audio-reactive, thinking-spin, lock-on)
│   ├── preview_card.py     Frosted Console card (mechanism + latency chip + undo toast)
│   ├── caption.py          live partial-transcript caption under the crosshair
│   ├── edge_glow.py        ambient screen-edge glow tied to SessionPhase
│   ├── diag_ring.py        radial connector-chain diagnostic ring
│   ├── history_overlay.py  command/undo history overlay
│   └── adaptive_ink.py     HUD ink auto-contrast for legibility
└── ax/
    └── ax_bridge.py        watchdog-wrapped Accessibility calls (timeout → fall-through)
```

---

## 5. Current state / next

- v2.0.0 built + 742 tests green + adversarial review (21 findings, all fixed) on `revamp/jarvis-v2`.
- Not yet merged to main, not tagged, not pushed to remote.
- Commits: `6cf5987` (v1.0.0 baseline) → `88f80bc` (P0 foundations) → `d995c07` (P2 integration) → `fde75ae` (P3: 21 fixes).

**Known deferred (do NOT claim fixed):** `mcp_connector.py` spawns a fresh stdio subprocess per tool call. Proper fix = long-lived `ClientSession` per MCP server + `shutdown_adapters()` in `app.run_live` finally block. MCP is opt-in (no servers configured by default), so this is a perf/cleanliness issue, not a correctness bug.

---

## 6. Conventions

- **Connectors never raise.** All errors come back inside `ConnectorResult`. The router always gets a result, never an exception.
- **Previews have zero side effects.** `preview()` builds the overlay card only — no system calls, no mutations.
- **Qt on main thread only.** All routing and execution runs on worker threads; any Qt widget update must be dispatched to the main thread.
- **Irreversible actions confirm first.** Anything with `risk=irreversible` or `must_confirm=True` blocks on the frosted console confirm gate before executing.
- **Speculative parse off Speech queue.** STT partials trigger rule-table parse speculatively so cheap-rung responses feel instantaneous.
- **`--dry-run` is the inspectable audit record** — use it as the regression spec, not print statements.

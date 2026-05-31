"""SessionPhase — the single source of truth for the HUD's lifecycle state (UI-01).

Every overlay surface (reticle orb, edge light, caption, frosted card, sound cues)
reads the SAME phase so the whole HUD moves with one personality instead of each
widget inferring state on its own. app.py owns a `phase = pyqtSignal(str)` on the
_Bridge and emits a transition at each pipeline boundary it already controls:

    _on_voice_utterance        -> HEARD
    resolve() begins           -> UNDERSTANDING
    router.run() begins        -> PLANNING
    confirm passed / execute    -> ACTING
    ConnectorResult.ok          -> DONE
    exception / ok == False     -> ERROR
    dismiss / settle            -> IDLE

This module is dependency-free and headless: it defines the vocabulary, the accent
palette (matte-black / cyan-neon JARVIS aesthetic, aligned with intent.RISK_* hues),
ordering, and tiny helpers. No Qt, no native imports — so connectors, the task
engine, and tests can speak phases without a display.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---- the closed phase vocabulary -------------------------------------------

IDLE = "idle"
LISTENING = "listening"
HEARD = "heard"
UNDERSTANDING = "understanding"
PLANNING = "planning"
ACTING = "acting"
DONE = "done"
ERROR = "error"

PHASES = (IDLE, LISTENING, HEARD, UNDERSTANDING, PLANNING, ACTING, DONE, ERROR)

#: phases that settle back to idle on their own (terminal, transient)
TERMINAL = (DONE, ERROR)
#: phases that mean "work is in flight" — drive the spinner / thinking motion
BUSY = (UNDERSTANDING, PLANNING, ACTING)


def is_phase(value: str) -> bool:
    return value in PHASES


def is_terminal(phase: str) -> bool:
    return phase in TERMINAL


def is_busy(phase: str) -> bool:
    return phase in BUSY


# ---- accent palette (RGB 0..255) -------------------------------------------
# Matte-black ground with cyan-neon spine; busy states walk cyan->violet->amber;
# terminal states use the mint/rose risk hues so phase and risk read coherently.

ACCENTS = {
    IDLE:          (96, 110, 130),    # dim slate — present but quiet
    LISTENING:     (34, 211, 238),    # cyan neon — the live mic signature
    HEARD:         (125, 230, 245),   # bright cyan-white — "got it"
    UNDERSTANDING: (139, 122, 255),   # violet — parsing/thinking
    PLANNING:      (167, 139, 250),   # lighter violet shimmer — routing
    ACTING:        (251, 191, 36),    # amber — an action is firing
    DONE:          (52, 211, 153),    # mint — success (== RISK_REVERSIBLE)
    ERROR:         (244, 99, 120),     # rose — failure (== RISK_IRREVERSIBLE)
}

_DEFAULT_ACCENT = (96, 110, 130)


def accent(phase: str) -> tuple:
    """RGB tuple for a phase; falls back to the idle slate for unknown phases."""
    return ACCENTS.get(phase, _DEFAULT_ACCENT)


def accent_hex(phase: str) -> str:
    r, g, b = accent(phase)
    return f"#{r:02x}{g:02x}{b:02x}"


@dataclass
class PhaseMeta:
    """Optional payload that rides alongside a phase transition (phase_meta signal).

    Carries the small bits a surface needs to render richly without re-deriving:
    the utterance heard, a free status line, the chosen connector/mechanism, an
    optional 0..1 progress, and a latency breakdown for the 'did it in Nms' badge.
    """
    phase: str = IDLE
    text: str = ""
    mechanism: str = ""
    pct: Optional[float] = None
    risk: str = ""
    latency: dict = field(default_factory=dict)  # {stt_ms, parse_ms, route_ms, exec_ms, total_ms}


__all__ = [
    "IDLE", "LISTENING", "HEARD", "UNDERSTANDING", "PLANNING", "ACTING", "DONE", "ERROR",
    "PHASES", "TERMINAL", "BUSY",
    "is_phase", "is_terminal", "is_busy",
    "ACCENTS", "accent", "accent_hex", "PhaseMeta",
]

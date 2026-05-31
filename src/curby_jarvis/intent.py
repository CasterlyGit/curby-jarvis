"""Frozen contracts shared by the router, connectors, fusion, and overlay.

Pure-Python and dependency-free on purpose: this module imports under CI with no
PyQt6, no pyobjc, no camera and no display. Every connector and test builds
against these dataclasses while the OS-touching code lives behind lazy imports
elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

# ---- vocabulary -------------------------------------------------------------

# The closed verb set the rule table + LLM parser lower utterances into. Adding a
# capability is a new row in a connector's can_handle, not a new Intent type.
VERBS = {
    "open", "run", "search", "mail",
    "play", "pause", "next", "prev", "volume", "mute",
    "close", "new", "new_tab", "save", "select_all", "fullscreen", "copy", "paste", "undo",
    "switch_tab", "goto_tab", "tab_by_name",
    "click_at", "move", "drag", "type",
    "agent_task",
}

# Risk drives the overlay's semantic color AND the auto-run/confirm decision.
RISK_REVERSIBLE = "reversible"      # mint  — auto-run if confident
RISK_IRREVERSIBLE = "irreversible"  # rose  — always confirm
RISK_AMBIGUOUS = "ambiguous"        # amber — confirm (low confidence / unresolved deixis)
RISK_LAUNCH = "launch"              # cyan  — app open/run

# Deictic tokens flip needs_pointer on at parse time.
DEICTIC_TOKENS = {"this", "that", "these", "those", "here", "there", "it"}

# Verbs whose effect is not trivially undoable -> force confirm.
IRREVERSIBLE_VERBS = {"close", "move", "drag", "save"}

# Verbs that are app-launch class -> cyan.
LAUNCH_VERBS = {"open", "run", "search", "mail"}

CONFIDENCE_FLOOR = 0.6  # below this, force confirm even for a reversible verb


def has_deictic(text: str) -> bool:
    """True if the utterance contains a deictic token (this/that/here/there/it)."""
    toks = {t.strip(".,!?;:'\"").lower() for t in (text or "").split()}
    return bool(toks & DEICTIC_TOKENS)


@dataclass(frozen=True)
class Intent:
    verb: str
    target: str = ""
    args: dict = field(default_factory=dict)
    needs_pointer: bool = False
    pointer: Optional[tuple] = None       # (screen_x, screen_y) logical px; bound by FusionBinder
    pointer2: Optional[tuple] = None      # destination for 'move THAT THERE'
    reversible: bool = True
    confidence: float = 1.0
    raw_utterance: str = ""

    def bound_with(self, pointer=None, pointer2=None) -> "Intent":
        """Return a frozen-safe copy with deixis point(s) bound."""
        return replace(
            self,
            pointer=pointer if pointer is not None else self.pointer,
            pointer2=pointer2 if pointer2 is not None else self.pointer2,
        )

    @property
    def risk(self) -> str:
        if self.verb in LAUNCH_VERBS:
            return RISK_LAUNCH
        if not self.reversible or self.verb in IRREVERSIBLE_VERBS:
            return RISK_IRREVERSIBLE
        if self.confidence < CONFIDENCE_FLOOR:
            return RISK_AMBIGUOUS
        if self.needs_pointer and self.pointer is None:
            return RISK_AMBIGUOUS
        return RISK_REVERSIBLE

    @property
    def must_confirm(self) -> bool:
        """Confirm model: auto-run ONLY when reversible + confident + resolved-deixis."""
        if not self.reversible or self.verb in IRREVERSIBLE_VERBS:
            return True
        if self.confidence < CONFIDENCE_FLOOR:
            return True
        if self.needs_pointer and self.pointer is None:
            return True
        return False


@dataclass
class PreviewCard:
    title: str
    gloss: str = ""
    mechanism: str = ""
    risk: str = RISK_REVERSIBLE
    target_rect: Optional[tuple] = None   # (x,y,w,h) logical px for the highlight bracket
    literal: str = ""                     # literal URL / AppleScript / coordinate, for audit


@dataclass
class ConnectorResult:
    ok: bool
    mechanism: str = ""
    latency_ms: float = 0.0
    error: str = ""        # 'secure_input_blocked' | 'ax_timeout' | 'no_press_action' | 'cancelled' | ...
    detail: str = ""

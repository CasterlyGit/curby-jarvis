"""Connector base class — the pluggable effector contract.

Every connector is independently buildable and headless-unit-testable against the
frozen Intent. OS-touching imports (pyobjc, subprocess, PyQt) are LAZY inside
methods so importing a connector never requires a display, camera, or permission.

The router orders the chain by (cost, -can_handle): cheapest confident wins.
Declared cost ranks:
    url=1, media_key=2, menubar_ax=3, ax_press=4, browser_osascript=6, llm=8, agent=10
"""
from __future__ import annotations

from ..intent import ConnectorResult, Intent, PreviewCard


class Connector:
    #: stable display name (also the mechanism tag in telemetry)
    name: str = "connector"
    #: declared cost rank — cheaper wins when confidence ties
    cost: int = 5

    def can_handle(self, intent: Intent) -> float:
        """0.0 if this connector cannot serve the verb, else a 0..1 confidence."""
        return 0.0

    def is_available(self, intent: Intent) -> bool:
        """Cheap correctness probe (scheme registered? app scriptable? AX trusted?
        not in Secure Input?) so a mis-declared cost surfaces instead of routing worse."""
        return True

    def preview(self, intent: Intent) -> PreviewCard:
        """Build the overlay card WITHOUT side effects."""
        return PreviewCard(title=intent.verb, mechanism=self.name, risk=intent.risk)

    def execute(self, intent: Intent) -> ConnectorResult:
        """Perform the action with an internal watchdog. Never raises — errors come
        back in ConnectorResult so the router can fall through to the next connector."""
        raise NotImplementedError


__all__ = ["Connector"]

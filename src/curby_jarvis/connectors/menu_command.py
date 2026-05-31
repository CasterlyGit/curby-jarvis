"""Menu-bar connector — the maintainable middle tier for app commands.

Maps the closed editing/window verbs (close/new/new_tab/save/select_all/
fullscreen/copy/paste/undo) onto the frontmost app's OWN menu bar via
ax_bridge.menu_command. Menu titles are localization-resolved and keystroke-free,
so this beats blind ⌘-chords whenever Accessibility is granted.

When the menu lookup misses (app exposes no matching title, or AX times out) we
fall to the CGEvent keystroke floor (cgevent.key). That floor is gated by Secure
Input inside cgevent itself, so a password field can't eat a key silently.

Headless-importable: cgevent and ax_bridge are the only OS surface, and the
cgevent import is LAZY inside execute() so this module imports under CI even
before the sibling cgevent.py lands.
"""
from __future__ import annotations

import time

from ..ax import ax_bridge
from ..intent import ConnectorResult, Intent, PreviewCard
from . import Connector

# Each menu verb -> (menu-title query for ax_bridge.menu_command, CGEvent keystroke floor).
# Title queries are the common English menu items; menu_command fuzzy-substring-matches
# them and is localization-tolerant via the app's own menu walk. The keystroke is the
# universal floor when no matching title exists or AX is untrusted.
_MENU_MAP: dict[str, tuple[str, str]] = {
    "close":      ("Close",            "cmd+w"),
    "new":        ("New",              "cmd+n"),
    "new_tab":    ("New Tab",          "cmd+t"),
    "save":       ("Save",             "cmd+s"),
    "select_all": ("Select All",       "cmd+a"),
    "fullscreen": ("Enter Full Screen", "ctrl+cmd+f"),
    "copy":       ("Copy",             "cmd+c"),
    "paste":      ("Paste",            "cmd+v"),
    "undo":       ("Undo",             "cmd+z"),
}


class MenuCommandConnector(Connector):
    name = "menubar_ax"
    cost = 3

    def can_handle(self, intent: Intent) -> float:
        # High confidence: these verbs ARE menu commands. Deictic motion verbs
        # (move/drag/click_at) belong to the deixis-click connector, not here.
        return 1.0 if intent.verb in _MENU_MAP else 0.0

    def is_available(self, intent: Intent) -> bool:
        # Available when Accessibility is granted (menu walk works). Even untrusted,
        # the cgevent floor can fire, but we declare availability on the AX spine so
        # the router prefers the cheaper, semantic menu path; cgevent stays the
        # in-execute fallback rather than the advertised mechanism.
        return ax_bridge.ax_available()

    def _query_and_combo(self, intent: Intent) -> tuple[str, str]:
        return _MENU_MAP.get(intent.verb, ("", ""))

    def preview(self, intent: Intent) -> PreviewCard:
        query, combo = self._query_and_combo(intent)
        _pid, app = ax_bridge.frontmost_pid_name()
        app = app or "frontmost app"
        # literal favors the menu query (what we'll actually try first); fall back to
        # the keystroke for verbs with no mapping (shouldn't happen for can_handle hits).
        literal = query or combo
        return PreviewCard(
            title=intent.verb,
            gloss=f"{intent.verb} via {app} menu",
            mechanism=self.name,
            risk=intent.risk,
            literal=literal,
        )

    def execute(self, intent: Intent) -> ConnectorResult:
        # Never raise: every failure mode returns a ConnectorResult so the router
        # can fall through. The two OS calls (menu_command, cgevent.key) are each
        # already watchdog/secure-input guarded in their own modules.
        t0 = time.monotonic()
        query, combo = self._query_and_combo(intent)
        if not query and not combo:
            return ConnectorResult(
                ok=False, mechanism=self.name,
                latency_ms=(time.monotonic() - t0) * 1000.0,
                error="unsupported_verb", detail=intent.verb,
            )

        # Tier A — semantic menu press (localization-proof, no synthetic keys).
        try:
            if query and ax_bridge.menu_command(query):
                return ConnectorResult(
                    ok=True, mechanism=self.name,
                    latency_ms=(time.monotonic() - t0) * 1000.0,
                    detail=f"menu:{query}",
                )
        except Exception as e:  # defensive: ax_bridge already guards, but never leak
            # fall through to the keystroke floor
            _ = e

        # Tier B — CGEvent keystroke floor. Lazy import so this module imports
        # headless even before cgevent.py exists on disk.
        try:
            from .. import cgevent
        except Exception as e:
            return ConnectorResult(
                ok=False, mechanism=self.name,
                latency_ms=(time.monotonic() - t0) * 1000.0,
                error="cgevent_unavailable", detail=str(e),
            )

        try:
            if cgevent.key(combo):
                return ConnectorResult(
                    ok=True, mechanism="cgevent_key",
                    latency_ms=(time.monotonic() - t0) * 1000.0,
                    detail=f"key:{combo}",
                )
            # cgevent.key returns False when Secure Input is engaged or send failed.
            return ConnectorResult(
                ok=False, mechanism="cgevent_key",
                latency_ms=(time.monotonic() - t0) * 1000.0,
                error="secure_input_blocked", detail=combo,
            )
        except Exception as e:
            return ConnectorResult(
                ok=False, mechanism="cgevent_key",
                latency_ms=(time.monotonic() - t0) * 1000.0,
                error="key_failed", detail=str(e),
            )


__all__ = ["MenuCommandConnector"]

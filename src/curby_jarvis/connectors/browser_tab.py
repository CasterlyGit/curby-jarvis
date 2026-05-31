"""BrowserTabConnector — switch/select browser tabs across the front browser.

Two mechanisms, two reliability tiers:

* switch_tab next/prev  -> cgevent.key("cmd+shift+]" / "cmd+shift+[").
  This is a SYSTEM keyboard shortcut honored by every Chromium browser AND Safari,
  so it needs ZERO Automation TCC and works the instant Accessibility/CGEvent is up.
  This is why this path is always-available and the connector's confident core.

* goto_tab / tab_by_name -> OsascriptBridge tells the front browser to
  "set active tab index" (Chromium) or select the named tab (Safari). This needs
  Automation TCC; if osascript reports a denial we degrade to ok=False
  error='automation_denied' rather than silently doing nothing.

cost=6 (browser_osascript tier) so cheaper menu/AX paths win when they can, but
this beats the LLM/agent fallback for tab control.

All OS-touching imports (cgevent, osascript, AppKit) are lazy -> headless import.
execute() never raises: every failure becomes a ConnectorResult.
"""
from __future__ import annotations

import time
from typing import Optional, Tuple

from ..intent import (
    RISK_REVERSIBLE,
    ConnectorResult,
    Intent,
    PreviewCard,
)
from . import Connector

# Front-browser bundle/name fragments we can drive. Chromium family shares the
# AppleScript dialect; Safari needs its own. Anything else -> CGEvent next/prev
# only (still works) but no by-name selection.
_CHROMIUM = ("chrome", "arc", "brave", "edge", "vivaldi", "opera", "chromium")
_SAFARI = ("safari",)

# System tab-cycle shortcuts (Chromium + Safari both honor these).
_KEY_NEXT = "cmd+shift+]"
_KEY_PREV = "cmd+shift+["


def _browser_kind(app_name: str) -> str:
    """Classify the front app name -> 'chromium' | 'safari' | ''."""
    n = (app_name or "").lower()
    if any(b in n for b in _SAFARI):
        return "safari"
    if any(b in n for b in _CHROMIUM):
        return "chromium"
    return ""


def _chromium_goto_index_script(app_name: str, index_1based: int) -> str:
    """AppleScript: set front window's active tab to a 1-based index (Chromium)."""
    return (
        f'tell application "{app_name}"\n'
        f"  set active tab index of front window to {int(index_1based)}\n"
        f"end tell"
    )


def _chromium_by_name_script(app_name: str, name_query: str) -> str:
    """AppleScript: find the first tab whose title contains name_query and select it
    by setting active tab index of the front window (Chromium)."""
    q = (name_query or "").replace('"', '\\"')
    return (
        f'tell application "{app_name}"\n'
        f"  set theTabs to tabs of front window\n"
        f"  repeat with i from 1 to count of theTabs\n"
        f'    if (title of item i of theTabs) contains "{q}" then\n'
        f"      set active tab index of front window to i\n"
        f"      return i\n"
        f"    end if\n"
        f"  end repeat\n"
        f"  return 0\n"
        f"end tell"
    )


def _safari_by_name_script(name_query: str) -> str:
    """AppleScript: select the first Safari tab whose name contains name_query."""
    q = (name_query or "").replace('"', '\\"')
    return (
        'tell application "Safari"\n'
        "  set theTabs to tabs of front window\n"
        "  repeat with i from 1 to count of theTabs\n"
        f'    if (name of item i of theTabs) contains "{q}" then\n'
        "      set current tab of front window to item i of theTabs\n"
        "      return i\n"
        "    end if\n"
        "  end repeat\n"
        "  return 0\n"
        "end tell"
    )


def _safari_goto_index_script(index_1based: int) -> str:
    """AppleScript: select Safari front-window tab by 1-based index."""
    return (
        'tell application "Safari"\n'
        f"  set current tab of front window to tab {int(index_1based)} of front window\n"
        "end tell"
    )


class BrowserTabConnector(Connector):
    name = "browser_tab"
    cost = 6

    def __init__(self, bridge=None):
        # Inject an OsascriptBridge for tests; default to the process-shared one
        # (lazily, so importing this connector pulls in nothing OS-touching).
        self._bridge = bridge

    def _get_bridge(self):
        if self._bridge is None:
            from ..osascript_bridge import shared
            self._bridge = shared()
        return self._bridge

    # -- routing --------------------------------------------------------------

    def can_handle(self, intent: Intent) -> float:
        if intent.verb == "switch_tab":
            return 0.95           # CGEvent shortcut: high, reliable
        if intent.verb in ("goto_tab", "tab_by_name"):
            return 0.8            # needs Automation; confident but gated on TCC
        return 0.0

    def is_available(self, intent: Intent) -> bool:
        # switch_tab rides CGEvent -> available unless Secure Input eats keystrokes.
        if intent.verb == "switch_tab":
            from ..ax.secure_input import secure_input_active
            return not secure_input_active()
        # by-name / goto are best-effort: claim availability and let execute() map
        # an Automation denial cleanly. Only hard-block under Secure Input is
        # unnecessary (osascript isn't synthetic input), so always best-effort True.
        return True

    # -- preview --------------------------------------------------------------

    def preview(self, intent: Intent) -> PreviewCard:
        if intent.verb == "switch_tab":
            d = (intent.args or {}).get("dir", "next")
            combo = _KEY_NEXT if d == "next" else _KEY_PREV
            return PreviewCard(
                title=f"{d} tab",
                gloss="front browser",
                mechanism=self.name,
                risk=RISK_REVERSIBLE,
                literal=combo,
            )
        # goto_tab / tab_by_name
        name, kind, app_name = self._resolve_target(intent)
        if intent.verb == "goto_tab":
            idx = self._goto_index(intent)
            literal = f"active tab index -> {idx}" if idx else "goto tab"
            title = f"go to tab {idx}" if idx else "go to tab"
        else:
            literal = f'tab containing "{name}"' if name else "tab by name"
            title = f"tab: {name}" if name else "tab by name"
        gloss = (app_name or "front browser") + (f" ({kind})" if kind else "")
        return PreviewCard(
            title=title,
            gloss=gloss,
            mechanism=self.name,
            risk=RISK_REVERSIBLE,
            literal=literal,
        )

    # -- execute --------------------------------------------------------------

    def execute(self, intent: Intent) -> ConnectorResult:
        t0 = time.time()
        try:
            if intent.verb == "switch_tab":
                return self._exec_switch(intent, t0)
            if intent.verb in ("goto_tab", "tab_by_name"):
                return self._exec_select(intent, t0)
            return ConnectorResult(
                ok=False, mechanism=self.name, error="unhandled_verb",
                latency_ms=(time.time() - t0) * 1000.0,
            )
        except Exception as e:  # never raise out of a connector
            return ConnectorResult(
                ok=False, mechanism=self.name, error="exception", detail=str(e),
                latency_ms=(time.time() - t0) * 1000.0,
            )

    def _exec_switch(self, intent: Intent, t0: float) -> ConnectorResult:
        from ..ax.secure_input import secure_input_active
        if secure_input_active():
            return ConnectorResult(
                ok=False, mechanism=self.name, error="secure_input_blocked",
                latency_ms=(time.time() - t0) * 1000.0,
            )
        d = (intent.args or {}).get("dir", "next")
        combo = _KEY_NEXT if d == "next" else _KEY_PREV
        from .. import cgevent  # lazy: built by the deixis-click task
        ok = bool(cgevent.key(combo))
        return ConnectorResult(
            ok=ok,
            mechanism=self.name,
            error="" if ok else "cgevent_blocked",
            detail=combo,
            latency_ms=(time.time() - t0) * 1000.0,
        )

    def _exec_select(self, intent: Intent, t0: float) -> ConnectorResult:
        name, kind, app_name = self._resolve_target(intent)
        if not kind:
            # Front app isn't a browser we can script. CGEvent can't select by name,
            # so surface a clean miss and let the chain fall through (LLM/agent).
            return ConnectorResult(
                ok=False, mechanism=self.name, error="not_a_browser",
                detail=app_name, latency_ms=(time.time() - t0) * 1000.0,
            )

        if intent.verb == "goto_tab":
            idx = self._goto_index(intent)
            if not idx:
                return ConnectorResult(
                    ok=False, mechanism=self.name, error="no_tab_index",
                    latency_ms=(time.time() - t0) * 1000.0,
                )
            script = (_safari_goto_index_script(idx) if kind == "safari"
                      else _chromium_goto_index_script(app_name, idx))
        else:  # tab_by_name
            if not name:
                return ConnectorResult(
                    ok=False, mechanism=self.name, error="no_tab_name",
                    latency_ms=(time.time() - t0) * 1000.0,
                )
            script = (_safari_by_name_script(name) if kind == "safari"
                      else _chromium_by_name_script(app_name, name))

        bridge = self._get_bridge()
        ok, out = bridge.run(script, timeout=1.0)
        latency = (time.time() - t0) * 1000.0
        if not ok:
            # 'automation_denied' | 'osascript_timeout' | stderr -> propagate.
            return ConnectorResult(
                ok=False, mechanism=self.name,
                error=out or "osascript_failed", latency_ms=latency,
            )
        # by-name returns the matched 1-based index, "0" == no tab matched.
        if intent.verb == "tab_by_name" and out.strip() in ("0", ""):
            return ConnectorResult(
                ok=False, mechanism=self.name, error="tab_not_found",
                detail=name, latency_ms=latency,
            )
        return ConnectorResult(
            ok=True, mechanism=self.name, detail=out, latency_ms=latency,
        )

    # -- helpers --------------------------------------------------------------

    def _resolve_target(self, intent: Intent) -> Tuple[str, str, str]:
        """Return (name_query, browser_kind, app_name) for the front app."""
        name = (intent.target or (intent.args or {}).get("name") or "").strip()
        app_name = self._front_app_name()
        return name, _browser_kind(app_name), app_name

    def _goto_index(self, intent: Intent) -> Optional[int]:
        """Extract a 1-based tab index from a goto_tab intent (args.index or a
        numeric target)."""
        args = intent.args or {}
        for v in (args.get("index"), args.get("tab"), intent.target):
            try:
                if v is None or v == "":
                    continue
                i = int(v)
                if i >= 1:
                    return i
            except (TypeError, ValueError):
                continue
        return None

    def _front_app_name(self) -> str:
        """Lazy AX lookup of the frontmost app name; '' headless / no permission."""
        try:
            from ..ax.ax_bridge import frontmost_pid_name
            _pid, name = frontmost_pid_name()
            return name or ""
        except Exception:
            return ""


__all__ = ["BrowserTabConnector"]

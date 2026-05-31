"""AppLaunchConnector — the zero-TCC tier-1 effector for open/run/search/mail.

This is the cheapest connector (cost=1) and needs NO permission grant: NSWorkspace
launches apps and opens URLs without Accessibility/Automation/Screen-Recording TCC.

  open/run -> resolve a spoken app name against a cached installed-app index
              (a one-time scan of the standard .app locations) and launch the
              bundle by URL via NSWorkspace.
  search   -> open the default browser to a Google search URL.
  mail     -> open a mailto: URL (the default mail client handles it).

Headless contract: NOTHING at import time touches AppKit. The Cocoa imports live
lazily inside _ns_workspace()/_scan_apps(); the index is built on first real use
(or injected pre-built for tests), so this module imports under CI with no display
and no permission. Every NSWorkspace call is wrapped in a thread+timeout watchdog
so a stuck LaunchServices can't freeze the controller, and execute() never raises.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional
from urllib.parse import quote_plus

from ..intent import ConnectorResult, Intent, PreviewCard
from . import Connector

# Standard bundle locations, scanned once and cached. WHY these three: covers
# user, system, and per-user installs; we deliberately do not recurse deeply
# (a shallow walk keeps the cold scan well under the watchdog budget).
_APP_DIRS = (
    "/Applications",
    "/System/Applications",
    "/System/Applications/Utilities",
    "/Applications/Utilities",
    os.path.expanduser("~/Applications"),
)

# Spoken aliases -> canonical lowercased bundle name. Speech rarely says the exact
# bundle name ("vs code" not "Visual Studio Code"), so a small alias map saves a
# round-trip to the LLM parser for the common apps.
_ALIASES = {
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "chrome": "google chrome",
    "browser": "safari",
    "terminal": "terminal",
    "code": "visual studio code",
    "calculator": "calculator",
    "settings": "system settings",
    "preferences": "system settings",
    "system preferences": "system settings",
}

_SEARCH_URL = "https://www.google.com/search?q="


def _with_timeout(fn, timeout: float, default=None):
    """Run fn() on a daemon thread; return its result or `default` on timeout.
    LaunchServices / NSWorkspace can wedge — never let one freeze the caller."""
    box = {"v": default, "done": False}

    def run():
        try:
            box["v"] = fn()
        except Exception:
            box["v"] = default
        finally:
            box["done"] = True

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return box["v"] if box["done"] else default


def _scan_apps() -> dict:
    """Build {lowercased app name -> absolute .app path} from the standard dirs.

    Shallow scan: top level of each dir plus one nested level (covers e.g.
    /Applications/Utilities). Pure os.listdir — no AppKit needed here, which keeps
    the scan cheap and the failure mode (missing dir) silent.
    """
    index: dict = {}
    for d in _APP_DIRS:
        try:
            entries = os.listdir(d)
        except (OSError, FileNotFoundError):
            continue
        for entry in entries:
            full = os.path.join(d, entry)
            if entry.endswith(".app"):
                name = entry[:-4].lower()
                index.setdefault(name, full)
            else:
                # one level deeper (e.g. nested Utilities) — best-effort, ignore errors
                try:
                    for sub in os.listdir(full):
                        if sub.endswith(".app"):
                            index.setdefault(sub[:-4].lower(), os.path.join(full, sub))
                except (OSError, FileNotFoundError):
                    continue
    return index


class AppLaunchConnector(Connector):
    """Tier-1 app launch + URL open. cost=1, no TCC."""

    name = "app_launch"
    cost = 1

    def __init__(self, app_index: Optional[dict] = None, scan_timeout: float = 2.0):
        # Injected index = testable without a filesystem scan. None => lazy scan
        # on first resolve so import (and construction) stay cheap and headless.
        self._index: Optional[dict] = dict(app_index) if app_index is not None else None
        self._scan_timeout = scan_timeout

    # -- index ---------------------------------------------------------------

    def _app_index(self) -> dict:
        if self._index is None:
            self._index = _with_timeout(_scan_apps, self._scan_timeout, default={}) or {}
        return self._index

    def _resolve_app(self, target: str) -> tuple[Optional[str], str]:
        """(bundle_path|None, resolved_name). Alias-map, exact, then substring match."""
        q = (target or "").strip().lower()
        if not q:
            return None, ""
        q = _ALIASES.get(q, q)
        idx = self._app_index()
        if q in idx:
            return idx[q], q
        # substring match: "spotify" should hit "spotify" even if spoken loosely;
        # prefer the shortest name containing the query (least-surprising target).
        hits = [(name, path) for name, path in idx.items() if q in name or name in q]
        if hits:
            hits.sort(key=lambda np: len(np[0]))
            return hits[0][1], hits[0][0]
        return None, q

    # -- contract ------------------------------------------------------------

    def can_handle(self, intent: Intent) -> float:
        if intent.verb in ("open", "run"):
            return 1.0 if (intent.target or "").strip() else 0.4
        if intent.verb in ("search", "mail"):
            return 1.0
        return 0.0

    def is_available(self, intent: Intent) -> bool:
        # NSWorkspace needs no permission grant — always available on macOS.
        return True

    # -- url builders --------------------------------------------------------

    def _search_url(self, intent: Intent) -> str:
        q = intent.args.get("query") or intent.target or ""
        return _SEARCH_URL + quote_plus(q.strip())

    def _mail_url(self, intent: Intent) -> str:
        to = (intent.args.get("to") or intent.target or "").strip()
        subj = (intent.args.get("subject") or "").strip()
        url = "mailto:" + quote_plus(to, safe="@.")
        if subj:
            url += "?subject=" + quote_plus(subj)
        return url

    def preview(self, intent: Intent) -> PreviewCard:
        if intent.verb in ("open", "run"):
            path, resolved = self._resolve_app(intent.target)
            if path:
                gloss = os.path.basename(path)[:-4] if path.endswith(".app") else resolved
                literal = path
            else:
                gloss = f"{resolved or intent.target} (not found)"
                literal = f"app:{resolved or intent.target}"
            return PreviewCard(title=f"open {intent.target or resolved}".strip(),
                               gloss=gloss, mechanism=self.name,
                               risk=intent.risk, literal=literal)
        if intent.verb == "search":
            url = self._search_url(intent)
            return PreviewCard(title=f"search {intent.target}".strip(),
                               gloss=intent.args.get("query") or intent.target,
                               mechanism=self.name, risk=intent.risk, literal=url)
        if intent.verb == "mail":
            url = self._mail_url(intent)
            return PreviewCard(title="new mail", gloss=intent.target or "compose",
                               mechanism=self.name, risk=intent.risk, literal=url)
        return PreviewCard(title=intent.verb, mechanism=self.name, risk=intent.risk)

    # -- execution -----------------------------------------------------------

    def execute(self, intent: Intent) -> ConnectorResult:
        t0 = time.time()
        try:
            if intent.verb in ("open", "run"):
                ok, detail = self._launch_app(intent)
            elif intent.verb == "search":
                ok, detail = self._open_url(self._search_url(intent))
            elif intent.verb == "mail":
                ok, detail = self._open_url(self._mail_url(intent))
            else:
                return ConnectorResult(ok=False, mechanism=self.name, error="unhandled_verb")
        except Exception as e:  # belt-and-suspenders: a connector must never raise
            return ConnectorResult(ok=False, mechanism=self.name, error="exception", detail=repr(e))
        lat = (time.time() - t0) * 1000.0
        if ok:
            return ConnectorResult(ok=True, mechanism=self.name, latency_ms=lat, detail=detail)
        return ConnectorResult(ok=False, mechanism=self.name, latency_ms=lat,
                               error=detail or "launch_failed")

    # -- native (all lazy) ---------------------------------------------------

    def _ns_workspace(self):
        from AppKit import NSWorkspace
        return NSWorkspace.sharedWorkspace()

    def _launch_app(self, intent: Intent) -> tuple[bool, str]:
        path, resolved = self._resolve_app(intent.target)
        if not path:
            # No bundle resolved — fail so the router can fall through (LLM/agent),
            # rather than blindly launching the wrong app.
            return False, f"app_not_found:{resolved or intent.target}"

        def _do() -> bool:
            from Foundation import NSURL
            ws = self._ns_workspace()
            url = NSURL.fileURLWithPath_(path)
            # Prefer the modern async API when present; fall back to the older sync
            # launcher on macOS versions that lack the configuration variant.
            if hasattr(ws, "openApplicationAtURL_configuration_completionHandler_"):
                from AppKit import NSWorkspaceOpenConfiguration
                cfg = NSWorkspaceOpenConfiguration.configuration()
                ws.openApplicationAtURL_configuration_completionHandler_(url, cfg, None)
                return True
            if hasattr(ws, "launchApplicationAtURL_options_configuration_error_"):
                ok, _err = ws.launchApplicationAtURL_options_configuration_error_(url, 0, {}, None)
                return bool(ok)
            return bool(ws.launchApplication_(path))

        ok = _with_timeout(_do, 4.0, default=False)
        return bool(ok), path

    def _open_url(self, url: str) -> tuple[bool, str]:
        def _do() -> bool:
            from Foundation import NSURL
            ns = NSURL.URLWithString_(url)
            if ns is None:
                return False
            return bool(self._ns_workspace().openURL_(ns))

        ok = _with_timeout(_do, 4.0, default=False)
        return bool(ok), url


__all__ = ["AppLaunchConnector"]

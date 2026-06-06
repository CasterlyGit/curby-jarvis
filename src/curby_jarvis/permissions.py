"""Permission probes + cache — know which connectors are usable before routing.

WHY: macOS TCC (Accessibility, Automation, Screen Recording) and the Claude CLI
availability gate every tier in the cost ladder. Probing at route time adds latency
and can trigger repeated OS dialogs. This module centralises all probes behind a
TTL cache so the router (and is_available checks) read from RAM on hot paths.

All native imports are LAZY (inside probe functions). Every probe returns a safe
default (False / 'unknown') if the import fails or the OS call errors — never
raises. `full_report()` aggregates everything into one dict with `all_green`.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, Optional


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------

def probe_accessibility() -> bool:
    """True if this process has the Accessibility (AX) trust grant.

    Calls ``ax_bridge.ax_available`` which is already lazy-wrapped with a
    thread+timeout watchdog. Falls back to False if the module isn't importable
    (e.g. CI, no pyobjc).
    """
    try:
        from .ax.ax_bridge import ax_available  # lazy via bridge
        return bool(ax_available())
    except Exception:
        return False


def probe_automation() -> str:
    """Probe Automation TCC via a benign osascript call.

    Returns:
        'authorized'  — osascript ran without a TCC denial.
        'denied'      — osascript returned an Automation-denied error.
        'unknown'     — osascript isn't present, timed out, or errored for
                        any other reason.
    """
    try:
        from .osascript_bridge import shared as osa_shared  # lazy
        bridge = osa_shared()
        # A benign, side-effect-free AppleScript that will fail with the exact
        # TCC phrasing if Automation is denied, or succeed silently otherwise.
        ok, out = bridge.run('tell application "System Events" to return "ok"', timeout=2.0)
        if ok:
            return "authorized"
        from .osascript_bridge import looks_like_tcc_denial
        if looks_like_tcc_denial(out):
            return "denied"
        return "unknown"
    except Exception:
        return "unknown"


def probe_screen_recording() -> bool:
    """True if Screen Recording permission is granted (CGPreflightScreenCaptureAccess).

    Uses the same Quartz call that ``screen.py`` uses internally. Falls back to
    True when Quartz isn't importable (non-macOS or non-native CI) so the screen
    tier isn't incorrectly flagged unavailable on Linux test runners.
    """
    try:
        from Quartz import CGPreflightScreenCaptureAccess  # lazy pyobjc
        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return True  # can't check → assume available


def request_screen_recording() -> None:
    """Ask macOS to show the Screen Recording permission dialog (CGRequestScreenCaptureAccess).

    Best-effort — no-op if Quartz isn't importable. Never raises.
    """
    try:
        from Quartz import CGRequestScreenCaptureAccess  # lazy pyobjc
        CGRequestScreenCaptureAccess()
    except Exception:
        pass


def probe_agent() -> Dict[str, Any]:
    """Check Claude CLI and API key availability.

    Returns a dict with:
        ``claude_cli``    — str path to the claude binary, or empty string.
        ``api_key``       — bool, True if ``ANTHROPIC_API_KEY`` is non-empty.
        ``cli_backend``   — bool, True if the local CLI is available as a
                            backend (CURBY_BACKEND=cli, or no key + claude on PATH).
        ``agent_usable``  — bool, True when ANY backend is usable (api_key OR
                            cli_backend). Used by ``--check`` to report readiness.
    """
    import shutil  # stdlib — always available
    cli_path_val = os.environ.get("CLAUDE_CLI") or ""
    if not cli_path_val:
        try:
            found = shutil.which("claude")
            cli_path_val = found or ""
        except Exception:
            cli_path_val = ""
    api_key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    # Probe CLI backend availability via the dedicated helper (lazy import).
    cli_backend = False
    try:
        from .claude_cli import backend_is_cli  # lazy — never at top level
        cli_backend = backend_is_cli()
    except Exception:
        cli_backend = bool(cli_path_val)  # degrade: available if binary found
    return {
        "claude_cli": cli_path_val,
        "api_key": api_key,
        "cli_backend": cli_backend,
        "agent_usable": api_key or cli_backend,
    }


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------

class PermissionCache:
    """Memoize permission probes with a time-to-live.

    Args:
        ttl: seconds before a cached value expires and the probe re-runs.
        clock: monotonic clock callable; inject a fake in tests.
    """

    def __init__(
        self,
        ttl: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._cache: Dict[str, Any] = {}
        self._ts: Dict[str, float] = {}

    def get(self, name: str, probe: Optional[Callable[[], Any]] = None) -> Any:
        """Return cached value for `name`, re-running `probe` if stale.

        `probe` defaults to the module-level ``probe_<name>`` function when
        omitted, so ``cache.get('accessibility')`` works without a second arg.

        Returns None if `name` is unknown and no probe provided.
        """
        now = self._clock()
        ts = self._ts.get(name)
        if ts is None or (now - ts) >= self._ttl:
            if probe is None:
                _probes = _PROBE_MAP()
                probe = _probes.get(name)
            if probe is None:
                return None
            try:
                result = probe()
            except Exception:
                result = None
            self._cache[name] = result
            self._ts[name] = now
        return self._cache.get(name)

    def invalidate(self) -> None:
        """Clear all cached values, forcing fresh probes on next get()."""
        self._cache.clear()
        self._ts.clear()


def _PROBE_MAP() -> Dict[str, Callable[[], Any]]:
    """Lazy map from probe name → callable. Built on demand to stay headless."""
    return {
        "accessibility": probe_accessibility,
        "automation": probe_automation,
        "screen_recording": probe_screen_recording,
        "agent": probe_agent,
    }


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------

def full_report() -> Dict[str, Any]:
    """Aggregate all probes into one dict, with an ``all_green`` summary.

    ``all_green`` is True only when every connector TIER is usable:
        - Tier AX (ax_press, menubar_ax): accessibility + automation
        - Tier screen: screen_recording
        - Tier agent: api_key present (claude_cli is nice-to-have not required)

    Best-effort — any individual probe exception is caught and treated as False.
    Never raises.
    """
    report: Dict[str, Any] = {}
    try:
        report["accessibility"] = probe_accessibility()
    except Exception:
        report["accessibility"] = False

    try:
        report["automation"] = probe_automation()
    except Exception:
        report["automation"] = "unknown"

    try:
        report["screen_recording"] = probe_screen_recording()
    except Exception:
        report["screen_recording"] = False

    try:
        report["agent"] = probe_agent()
    except Exception:
        report["agent"] = {"claude_cli": "", "api_key": False}

    # all_green: every tier is usable.
    # Agent tier is green when EITHER an API key is set OR the local claude CLI
    # is available as a backend (CURBY_BACKEND=cli or no key + claude on PATH).
    ax_ok = bool(report["accessibility"])
    auto_ok = report["automation"] == "authorized"
    screen_ok = bool(report["screen_recording"])
    agent_data = report["agent"] or {}
    agent_ok = bool(agent_data.get("agent_usable", agent_data.get("api_key", False)))

    report["all_green"] = ax_ok and auto_ok and screen_ok and agent_ok
    return report


__all__ = [
    "probe_accessibility",
    "probe_automation",
    "probe_screen_recording",
    "request_screen_recording",
    "probe_agent",
    "PermissionCache",
    "full_report",
]

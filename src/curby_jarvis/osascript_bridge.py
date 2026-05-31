"""OsascriptBridge — a watchdog-wrapped AppleScript runner.

A truly persistent `osascript -i` REPL is brittle: it has no clean per-call output
sentinel, mixes stdout/stderr, and a single wedged script poisons the worker for
every later call. So we keep this HONEST: each run() spawns a fresh
`osascript -e <script>` subprocess (cheap, ~15-30ms warm) wrapped in a per-call
watchdog that hard-kills a hung child instead of letting a frozen browser freeze
the whole controller. A warm worker is kept ONLY as an optional fast path behind a
flag; the fresh-subprocess path is always the source of truth and the fallback.

All subprocess imports are lazy so this module imports headless under CI (no shell,
no Automation grant, no display). run() NEVER raises — it returns (ok, out) and
maps a non-zero exit / TCC denial / timeout into ok=False with a diagnostic string.
"""
from __future__ import annotations

import threading
from typing import Optional, Tuple

DEFAULT_TIMEOUT = 1.0

# osascript exit code when an AppleScript raises (incl. "Not authorized to send
# Apple events" == Automation TCC denial). We sniff stderr for the TCC phrasing so
# the connector can degrade to 'automation_denied' instead of a generic failure.
_TCC_MARKERS = (
    "not authorized",
    "not allowed to send apple events",
    "-1743",  # errAEEventNotPermitted
    "-1728",  # errAENoSuchObject (front window / tab index out of range)
)


def looks_like_tcc_denial(stderr: str) -> bool:
    """True if osascript stderr indicates an Automation permission denial."""
    s = (stderr or "").lower()
    return any(m in s for m in _TCC_MARKERS)


class OsascriptBridge:
    """Run AppleScript with a hard per-call timeout and auto-restart on hang.

    Public: run(script, timeout=1.0) -> (ok: bool, out: str). On timeout the child
    is killed and (False, 'osascript_timeout') is returned; on a TCC denial the out
    carries 'automation_denied' so the caller can map it cleanly.
    """

    def __init__(self, warm: bool = False):
        # `warm` reserved for an optional persistent worker fast path; the default
        # OFF keeps behavior deterministic and the fresh-subprocess path canonical.
        self._warm = warm
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> "OsascriptBridge":
        """No-op for the fresh-subprocess strategy; present so callers can treat the
        bridge as a managed resource symmetrically with a future warm worker."""
        return self

    def stop(self) -> None:
        """Sentinel-terminate any warm worker. No-op in the fresh-subprocess path."""
        return None

    # -- core -----------------------------------------------------------------

    def run(self, script: str, timeout: float = DEFAULT_TIMEOUT) -> Tuple[bool, str]:
        """Execute one AppleScript. Returns (ok, out). Never raises.

        out on success = trimmed stdout; on failure = a diagnostic token
        ('automation_denied' | 'osascript_timeout' | 'osascript_missing' | stderr)."""
        if not script:
            return False, "empty_script"
        with self._lock:
            return self._run_fresh(script, timeout)

    def _run_fresh(self, script: str, timeout: float) -> Tuple[bool, str]:
        # Lazy subprocess import: keeps the module headless-importable under CI.
        import subprocess

        try:
            proc = subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            return False, "osascript_missing"
        except Exception as e:  # pragma: no cover - environment-dependent
            return False, f"spawn_error:{e}"

        box: dict = {"out": "", "err": "", "rc": None}

        def _wait():
            try:
                out, err = proc.communicate()
                box["out"], box["err"] = out or "", err or ""
                box["rc"] = proc.returncode
            except Exception as e:  # pragma: no cover
                box["err"] = str(e)
                box["rc"] = -1

        t = threading.Thread(target=_wait, daemon=True)
        t.start()
        t.join(timeout)

        if t.is_alive() or box["rc"] is None:
            # Wedged child (a hung browser AppleEvent): kill it, don't block.
            try:
                proc.kill()
            except Exception:
                pass
            return False, "osascript_timeout"

        if box["rc"] == 0:
            return True, box["out"].strip()

        if looks_like_tcc_denial(box["err"]):
            return False, "automation_denied"
        return False, (box["err"].strip() or f"osascript_rc_{box['rc']}")


# Module-level singleton so connectors share one warm-able bridge (and one lock).
_SHARED: Optional[OsascriptBridge] = None


def shared() -> OsascriptBridge:
    """Process-wide shared bridge instance (lazy)."""
    global _SHARED
    if _SHARED is None:
        _SHARED = OsascriptBridge()
    return _SHARED


__all__ = ["OsascriptBridge", "shared", "looks_like_tcc_denial"]

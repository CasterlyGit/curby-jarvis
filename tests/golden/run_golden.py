"""Golden-demo harness — drive the 8 point-and-say demos through the real CLI.

Each demo is run as a SEPARATE subprocess exactly the way a user (and the demo
script) would: `python src/curby_jarvis/app.py --say "<u>" --dry-run [--pointer ...]`.
We assert the audit JSON's chosen connector / risk / must_confirm match the frozen
expectation. Running through the actual CLI (not an in-process call) is the point:
it proves the run-as-script bootstrap, argparse, the dry-run path, and the router
all wire together end-to-end and stay headless (no display / camera / key / perm).

GD1 open Spotify         -> app_launch   / launch        / no-confirm
GD2 mute                 -> media_key    / reversible    / no-confirm
GD3 close this window    -> menubar_ax   / irreversible  / confirm
GD4 next tab             -> browser_tab  / reversible    / no-confirm
GD5 play this (+pointer) -> deixis_click / (deixis)      / -
GD6 move that there      -> deixis_click / irreversible* / confirm   (*ambiguous until AX resolves)
GD7 play this (NO ptr)   -> deixis_click / ambiguous     / confirm
GD8 long open-ended      -> agent_loop     / ambiguous   / -          (rule miss -> parser None -> agent loop)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

# Repo root = two levels up from this file (tests/golden/ -> repo).
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_APP = os.path.join(_REPO, "src", "curby_jarvis", "app.py")


@dataclass(frozen=True)
class GoldenDemo:
    gid: str
    utterance: str
    pointer: Optional[str] = None
    pointer2: Optional[str] = None
    # Expectations:
    chosen_connector: str = ""
    verb: str = ""
    must_confirm: Optional[bool] = None
    # accepted risk values (a set so GD5/GD6 can tolerate the headless AX-miss ->
    # 'ambiguous' while still passing on a grant where the element resolves).
    risk_in: tuple = field(default_factory=tuple)


# The 8 frozen golden demos. Deixis demos inject --pointer so they're testable
# fully headless (no live gesture stream).
GOLDEN: list[GoldenDemo] = [
    GoldenDemo("GD1", "open Spotify",
               chosen_connector="app_launch", verb="open",
               must_confirm=False, risk_in=("launch",)),
    GoldenDemo("GD2", "mute",
               chosen_connector="media_key", verb="mute",
               must_confirm=False, risk_in=("reversible",)),
    GoldenDemo("GD3", "close this window",
               chosen_connector="menubar_ax", verb="close",
               must_confirm=True, risk_in=("irreversible",)),
    GoldenDemo("GD4", "next tab",
               chosen_connector="browser_tab", verb="switch_tab",
               must_confirm=False, risk_in=("reversible",)),
    GoldenDemo("GD5", "play this", pointer="700,400",
               chosen_connector="deixis_click", verb="play",
               # with a bound pointer but no AX grant the element is unresolved ->
               # ambiguous; with a grant + a real element it would be reversible.
               risk_in=("ambiguous", "reversible")),
    GoldenDemo("GD6", "move that there", pointer="300,300", pointer2="800,600",
               chosen_connector="deixis_click", verb="move",
               must_confirm=True, risk_in=("irreversible", "ambiguous")),
    GoldenDemo("GD7", "play this",
               chosen_connector="deixis_click", verb="play",
               must_confirm=True, risk_in=("ambiguous",)),
    GoldenDemo("GD8", "reorganize my entire downloads folder by file type and date",
               # Revamp: the open-ended path now routes to the structured agent_loop
               # (cost 9) — a Claude tool-use loop over the whole connector palette —
               # which interposes BEFORE the black-box agent_fallback (cost 10).
               chosen_connector="agent_loop", verb="agent_task",
               must_confirm=False, risk_in=("ambiguous",)),
]


def run_demo(demo: GoldenDemo) -> dict:
    """Run one demo through the real CLI and return its parsed audit dict.

    Forces an empty ANTHROPIC_API_KEY so GD8 deterministically takes the
    rule-miss -> parser-returns-None -> agent path with no network. In --dry-run the
    intended route (cheapest can_handle, availability ignored) is the agent_loop.
    """
    argv = [sys.executable, _APP, "--say", demo.utterance, "--dry-run"]
    if demo.pointer:
        argv += ["--pointer", demo.pointer]
    if demo.pointer2:
        argv += ["--pointer2", demo.pointer2]

    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # keep the harness offline + deterministic

    proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(
            f"{demo.gid} exited {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    # The dry-run prints exactly one JSON line; take the last non-empty line so any
    # stray warning above it doesn't break parsing.
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        raise AssertionError(f"{demo.gid} produced no output\nstderr={proc.stderr}")
    return json.loads(lines[-1])


def check_demo(demo: GoldenDemo) -> list[str]:
    """Run a demo and return a list of human-readable failures ([] == pass)."""
    audit = run_demo(demo)
    fails: list[str] = []
    if audit.get("chosen_connector") != demo.chosen_connector:
        fails.append(
            f"connector: expected {demo.chosen_connector!r}, got {audit.get('chosen_connector')!r}")
    if demo.verb and audit.get("verb") != demo.verb:
        fails.append(f"verb: expected {demo.verb!r}, got {audit.get('verb')!r}")
    if demo.must_confirm is not None and audit.get("must_confirm") != demo.must_confirm:
        fails.append(
            f"must_confirm: expected {demo.must_confirm}, got {audit.get('must_confirm')}")
    if demo.risk_in and audit.get("risk") not in demo.risk_in:
        fails.append(f"risk: expected one of {demo.risk_in}, got {audit.get('risk')!r}")
    return fails


def main() -> int:
    """CLI entry: run all demos, print a table, exit non-zero on any failure."""
    rc = 0
    for demo in GOLDEN:
        audit = run_demo(demo)
        fails = check_demo(demo)
        status = "PASS" if not fails else "FAIL"
        print(f"[{status}] {demo.gid} {demo.utterance!r} -> "
              f"{audit.get('chosen_connector')} / {audit.get('risk')} / "
              f"confirm={audit.get('must_confirm')}")
        for f in fails:
            print(f"        {f}")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

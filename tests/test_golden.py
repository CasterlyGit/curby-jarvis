"""Headless golden-demo gate — the 8 point-and-say demos must route correctly.

Each demo is driven through the REAL CLI (`python src/curby_jarvis/app.py --say ...
--dry-run`) by tests/golden/run_golden.py, with deixis demos injecting --pointer so
the whole thing runs with no display, no camera, no mic, no API key, no permission.
This is the end-to-end wiring proof for app.py: bootstrap + argparse + resolve +
deixis-bind + router selection + connector preview, all stitched together.
"""
from __future__ import annotations

import os
import sys

import pytest

# Make tests/golden/ importable regardless of how pytest collects this file.
_HERE = os.path.dirname(os.path.abspath(__file__))
_GOLDEN = os.path.join(_HERE, "golden")
if _GOLDEN not in sys.path:
    sys.path.insert(0, _GOLDEN)

import run_golden  # noqa: E402


@pytest.mark.parametrize("demo", run_golden.GOLDEN, ids=[d.gid for d in run_golden.GOLDEN])
def test_golden_demo_routes_correctly(demo):
    fails = run_golden.check_demo(demo)
    assert not fails, f"{demo.gid} {demo.utterance!r}: " + "; ".join(fails)


def test_all_eight_demos_present():
    """Guard: exactly GD1..GD8 are covered (a dropped demo is a regression)."""
    gids = {d.gid for d in run_golden.GOLDEN}
    assert gids == {f"GD{i}" for i in range(1, 9)}

"""Shared pytest fixtures / path setup for the curby-jarvis suite.

pyproject already sets pythonpath=["src"], but we also insert src/ here so the
golden harness (which shells out to `python src/curby_jarvis/app.py`) and any
direct `import curby_jarvis` in a test resolve the package without an install,
even if a runner ignores the ini pythonpath.
"""
from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

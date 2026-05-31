"""Overlay widgets for curby-jarvis (Frosted Console aesthetic).

GUI-only package: every PyQt6 import lives lazily inside methods so the package
imports headless under CI (no display, no Qt event loop). Pure geometry helpers
live at module scope so the bracket math is unit-testable without a widget.
"""

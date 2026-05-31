"""Pointer (deixis) subsystem — gesture-stream consumer, calibration, fusion.

Pure-Python re-exports only; the websockets/asyncio machinery is lazy inside
ws_client so importing this package never opens a socket or needs a display.
"""
from __future__ import annotations

from .ws_client import PointerSample, PointerStream  # noqa: F401

__all__ = ["PointerSample", "PointerStream"]

"""curby-jarvis — voice + hand-gesture universal computer controller for macOS.

Public surface is the frozen contract (intent) and the CapabilityRouter; the
OS-touching pieces (AX, CGEvent, overlay, camera) live behind lazy imports so the
core imports headless under CI.
"""
from .intent import Intent, PreviewCard, ConnectorResult  # noqa: F401
from .router import CapabilityRouter  # noqa: F401

__version__ = "1.0.0"

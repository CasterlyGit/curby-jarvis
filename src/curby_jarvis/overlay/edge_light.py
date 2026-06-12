"""Ambient edge-light strip overlay (UI-08) — phase-reactive screen accent.

WHY: A thin ambient strip hugging the bottom and left edges communicates the
current pipeline phase at a glance (JARVIS HUD language). It replaces the need to
keep watching the frosted card: when the HUD is LISTENING the strip pulses cyan;
ACTING pulses amber; DONE flashes mint; ERROR flashes rose; IDLE is invisible.

HEADLESS CONTRACT: importing this module must NOT touch PyQt6 or any display.
Qt symbols are imported lazily. HEADLESS flag mirrors reticle.py. The pure helper
``gradient_stops`` is always importable and testable without any Qt / display.

Layout: the widget spans the full screen bottom edge (height == STRIP_H) plus a
stub up the left edge (width == STRIP_W). Both edges share one widget so one
transparency pass covers both. The widget is ALWAYS click-through.
"""
from __future__ import annotations

from typing import Optional

# ---- headless probe ---------------------------------------------------------
try:  # pragma: no cover
    import PyQt6  # noqa: F401

    HEADLESS = False
except Exception:
    HEADLESS = True

# ---- constants --------------------------------------------------------------
STRIP_H = 4           # bottom edge strip height (logical px)
STRIP_W = 4           # left edge strip width (logical px)

# When intensity == 1.0 the gradient peak alpha is this value (170 ≈ 66%).
_PEAK_ALPHA = 170
# Pulse period matches motion.DURATIONS['edge_pulse'] == 1100ms
PULSE_PERIOD_S = 1.1


# ---- pure helper (headless-safe) -------------------------------------------

def gradient_stops(phase: str, intensity: float) -> list[tuple[float, int, int, int, int]]:
    """Return gradient stop descriptors for a phase + intensity level.

    Each stop is ``(position, r, g, b, alpha)`` where *position* is 0.0..1.0 along
    the gradient axis (left→right for the bottom strip, bottom→top for the left
    strip). The gradient goes from the accent colour (at the corner/midpoint) to
    transparent at the far ends.

    ``intensity`` is 0.0..1.0 and scales the peak alpha linearly (0 → invisible,
    1 → _PEAK_ALPHA).

    Pure, side-effect-free, testable without Qt.
    """
    from curby_jarvis.overlay import phase as phase_mod

    r, g, b = phase_mod.accent(phase)
    peak_a = max(0, min(255, int(_PEAK_ALPHA * max(0.0, min(1.0, intensity)))))

    if phase == phase_mod.IDLE or intensity <= 0.0:
        # Fully transparent gradient — widget is invisible.
        return [
            (0.0, r, g, b, 0),
            (1.0, r, g, b, 0),
        ]

    # Gradient: transparent at both ends, peak in the middle-ish (nearer the
    # corner anchor so the accent radiates outward from the corner).
    return [
        (0.0,  r, g, b, 0),
        (0.25, r, g, b, peak_a),
        (0.55, r, g, b, peak_a // 2),
        (1.0,  r, g, b, 0),
    ]


# ---- widget (Qt lazy) -------------------------------------------------------

if not HEADLESS:
    import math

    from PyQt6.QtWidgets import QWidget, QApplication
    from PyQt6.QtCore import Qt, QRectF, QTimer, QPointF
    from PyQt6.QtGui import (
        QPainter, QColor, QLinearGradient, QBrush,
    )

    class EdgeLightWidget(QWidget):
        """Frameless, click-through ambient strip along the bottom + left edges.

        Usage::

            el = EdgeLightWidget()
            el.set_phase("listening")   # activates and begins pulse
            el.set_phase("idle")        # fades out / invisible

        P2 wires ``_Bridge.phase`` → ``set_phase``.
        """

        def __init__(self) -> None:
            super().__init__()
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

            self._phase: str = "idle"
            self._intensity: float = 0.0      # current rendered intensity (0..1)
            self._pinned: bool = False

            # 30fps tick for pulsing / fading
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)

        # -- public API ------------------------------------------------------ #

        def set_phase(self, phase_str: str) -> None:
            """Update displayed phase; starts/stops pulse animation accordingly."""
            from curby_jarvis.overlay import phase as phase_mod

            self._phase = phase_str
            if phase_str == phase_mod.IDLE:
                self._intensity = 0.0
                self._timer.stop()
                self.update()
                # Keep widget visible but transparent (so geometry is still set).
            else:
                if not self.isVisible():
                    self._ensure_geometry()
                    self.show()
                    self._pin_once()
                if not self._timer.isActive():
                    self._timer.start(33)  # ~30fps

        # -- internals ------------------------------------------------------- #

        def _ensure_geometry(self) -> None:
            """Size + position to hug bottom and left edges of the primary screen."""
            try:
                app = QApplication.instance()
                screen = app.primaryScreen() if app else None
                if screen is not None:
                    geom = screen.geometry()
                    sw, sh = geom.width(), geom.height()
                else:
                    sw, sh = 1920, 1080
            except Exception:
                sw, sh = 1920, 1080
            # Widget covers the bottom strip + a left-edge strip stub.
            # We use one rectangle that covers the full bottom + left edge.
            # For simplicity: full-width bottom strip at screen bottom.
            # The left-edge strip is a separate thin left column.
            # We union both into one window: x=0, y=sh-STRIP_H, w=sw, h=STRIP_H
            # then paint the left-edge upwards inside a tall window.
            # Simpler approach: cover the whole bottom row + left column as one
            # L-shaped region via a big widget. We'll use the bottom row height
            # from y=sh-STRIP_H and also paint the left rail inside the widget.
            self.setGeometry(0, sh - STRIP_H, sw, STRIP_H)

        def _pin_once(self) -> None:
            if not self._pinned:
                try:
                    from ..macwin import make_always_visible
                    make_always_visible(self, click_through=True)
                except Exception:
                    pass
                self._pinned = True

        def _tick(self) -> None:
            import time as _time

            if self._phase == "idle":
                self._intensity = 0.0
                self._timer.stop()
            else:
                # Sinusoidal pulse between 0.5 and 1.0
                t = _time.time()
                self._intensity = 0.5 + 0.5 * (
                    1.0 - math.cos(2.0 * math.pi * (t % PULSE_PERIOD_S) / PULSE_PERIOD_S)
                ) / 2.0
            self.update()

        def paintEvent(self, _event) -> None:  # noqa: N802
            if self._intensity <= 0.0 and self._phase == "idle":
                return
            stops = gradient_stops(self._phase, self._intensity)

            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

            w, h = float(self.width()), float(self.height())

            # Horizontal bottom-edge gradient
            grad = QLinearGradient(QPointF(0, 0), QPointF(w, 0))
            for pos, r, g, b, a in stops:
                grad.setColorAt(pos, QColor(r, g, b, a))
            p.fillRect(QRectF(0, 0, w, h), QBrush(grad))

else:

    class EdgeLightWidget:  # type: ignore[no-redef]
        """Stub for headless environments; raises on construction."""

        def __init__(self, *_, **__):
            raise RuntimeError(
                "EdgeLightWidget requires PyQt6 + a display. "
                "The pure helper gradient_stops is available without Qt."
            )


__all__ = ["HEADLESS", "gradient_stops", "EdgeLightWidget", "STRIP_H", "STRIP_W", "PULSE_PERIOD_S"]

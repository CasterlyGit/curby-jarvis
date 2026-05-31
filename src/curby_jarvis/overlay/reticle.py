"""Click-through deixis overlay: a self-contrasting pointer RETICLE and a TARGET
BRACKET drawn so they stay legible over ANY background.

Two things touch raw pixels here, both using curby's action_highlight technique
(see curby/src/action_highlight.py): a WHITE stroke laid over a 1px BLACK keyline
so the mark survives a white doc AND a dark video AND a busy photo. The Frosted
Console card draws elsewhere; this widget is ONLY the reticle + bracket layer, so
it can be click-through and sit directly under the tracked fingertip.

HEADLESS CONTRACT: the module imports with PyQt6 absent — every Qt symbol is
imported lazily inside methods. `HEADLESS` is True when PyQt6 cannot be imported.
The bracket geometry (`bracket_segments`) and the breathing-ring math
(`breathe_phase`) are PURE functions, unit-testable with no QApplication.

Coordinates are LOGICAL pixels (Qt geometry == AX == CGEvent space, per curby's
screen_capture convention), so a point that is clickable is paintable at the same
(x, y) with no DPI correction.
"""
from __future__ import annotations

import math
from typing import Optional

# ---- headless probe ---------------------------------------------------------
# Touch only the import system, never a QApplication, so importing this module on
# a headless CI box (no display) cannot crash or spin up Qt.
try:  # pragma: no cover - trivial availability flag
    import PyQt6  # noqa: F401

    HEADLESS = False
except Exception:  # PyQt6 not installed -> module still imports, widget unusable
    HEADLESS = True


# ---- aesthetic constants (Frosted Console; locked) --------------------------
# Semantic risk -> accent RGB. Matches intent.RISK_* and the overlay spec.
RISK_COLORS: dict[str, tuple[int, int, int]] = {
    "launch": (0x00, 0xE4, 0xFF),        # cyan    — app open/run
    "reversible": (0x2E, 0xE5, 0x9D),    # mint     — auto-runnable
    "irreversible": (0xFF, 0x5B, 0x8A),  # rose     — destructive, always confirm
    "ambiguous": (0xF0, 0xA5, 0x00),     # amber    — unresolved deixis / low conf
}
_DEFAULT_ACCENT = (0xB0, 0x8E, 0xFF)     # soft purple — neutral / risk unset

# Reticle ring radii (logical px). Inner ring is the fixed crosshair; the
# breathing ring expands/contracts on a 1.2s cycle.
_RING_R = 13.0
_BREATHE_MIN_R = 18.0
_BREATHE_MAX_R = 30.0
BREATHE_PERIOD_S = 1.2

# Bracket corner-arm length is a fraction of the smaller rect side, clamped so
# tiny targets still read as a frame and huge ones don't grow silly long arms.
_BRACKET_FRAC = 0.22
_BRACKET_MIN = 10.0
_BRACKET_MAX = 26.0

# A generous pad so the breathing ring / bracket arms never clip the window edge.
_PAD = 60


# ---- pure geometry (no Qt) --------------------------------------------------

def bracket_segments(rect) -> list[tuple[float, float, float, float]]:
    """Return the 8 line segments (x1, y1, x2, y2) of a corner-bracket frame.

    Two short arms per corner (horizontal + vertical), four corners -> 8 lines.
    `rect` is (x, y, w, h) in any coordinate space; segments come back in that
    SAME space, so the caller can paint them widget-local or screen-absolute.
    Pure + side-effect-free so the math is asserted without a widget.
    """
    x, y, w, h = (float(v) for v in rect)
    # Degenerate rects still yield a (tiny) frame rather than NaNs.
    w = max(w, 1.0)
    h = max(h, 1.0)
    arm = max(_BRACKET_MIN, min(_BRACKET_MAX, min(w, h) * _BRACKET_FRAC))
    # Never let arms overshoot past the rect's own half-extent (small targets).
    arm = min(arm, w / 2.0, h / 2.0)

    L, T = x, y
    R, B = x + w, y + h
    return [
        # top-left
        (L, T, L + arm, T),
        (L, T, L, T + arm),
        # top-right
        (R, T, R - arm, T),
        (R, T, R, T + arm),
        # bottom-right
        (R, B, R - arm, B),
        (R, B, R, B - arm),
        # bottom-left
        (L, B, L + arm, B),
        (L, B, L, B - arm),
    ]


def breathe_phase(now: float, period: float = BREATHE_PERIOD_S) -> float:
    """0..1 breathing value on a `period`-second cosine cycle (smooth, no jerk)."""
    return (1.0 - math.cos(2.0 * math.pi * (now % period) / period)) / 2.0


def breathe_radius(now: float, period: float = BREATHE_PERIOD_S) -> float:
    """Breathing-ring radius in logical px for the current time."""
    return _BREATHE_MIN_R + (_BREATHE_MAX_R - _BREATHE_MIN_R) * breathe_phase(now, period)


def accent_for(risk: Optional[str]) -> tuple[int, int, int]:
    """Semantic accent RGB for a risk label; purple fallback when unset/unknown."""
    return RISK_COLORS.get(risk or "", _DEFAULT_ACCENT)


# ---- widget (Qt lazy) -------------------------------------------------------
# Defined only when PyQt6 imported, so a headless `import` never references a Qt
# base class. Headless callers get a clear error if they try to construct it.

if not HEADLESS:
    from PyQt6.QtWidgets import QWidget, QApplication
    from PyQt6.QtCore import Qt, QPointF, QRectF, QTimer
    from PyQt6.QtGui import QPainter, QColor, QPen

    class ReticleWidget(QWidget):
        """Frameless, click-through reticle + target-bracket overlay.

        One widget shows EITHER the pointer reticle (`show_reticle`) OR a target
        bracket (`show_target`) at a time; `set_risk` tints the breathing ring
        and bracket. The window is sized to a tight box around the mark plus a
        pad, so translucent compositing stays cheap on Retina (the lag lesson
        from curby's action_highlight).
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

            self._mode: Optional[str] = None        # 'reticle' | 'target' | None
            self._point: Optional[tuple] = None      # screen-absolute (x, y)
            self._rect: Optional[tuple] = None       # screen-absolute (x, y, w, h)
            self._risk: Optional[str] = None
            self._origin = (0.0, 0.0)                # window top-left (screen abs)

            # 30fps tick drives the breathing ring; stops itself when hidden.
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)

            self._pinned = False  # macwin click-through applied once, post-show

        # -- public API ---------------------------------------------------- #

        def show_reticle(self, x: float, y: float) -> None:
            """Show the breathing pointer reticle centered on logical (x, y)."""
            self._mode = "reticle"
            self._point = (float(x), float(y))
            self._rect = None
            half = _BREATHE_MAX_R + _PAD
            self._place(x - half, y - half, 2 * half, 2 * half)

        def show_target(self, rect) -> None:
            """Show the corner-bracket frame around a logical (x, y, w, h) rect."""
            rx, ry, rw, rh = (float(v) for v in rect)
            self._mode = "target"
            self._rect = (rx, ry, rw, rh)
            self._point = None
            self._place(rx - _PAD, ry - _PAD, rw + 2 * _PAD, rh + 2 * _PAD)

        def hide(self) -> None:  # noqa: A003 - mirrors QWidget.hide name on purpose
            self._mode = None
            self._timer.stop()
            super().hide()

        def set_risk(self, risk: Optional[str]) -> None:
            """Tint the reticle/bracket with the semantic risk color."""
            self._risk = risk
            if self.isVisible():
                self.update()

        # -- internals ----------------------------------------------------- #

        def _place(self, gx: float, gy: float, gw: float, gh: float) -> None:
            """Position the window box and (re)show, keeping it always-on-top and
            click-through. Geometry stays in logical px so paint == click space."""
            self._origin = (gx, gy)
            self.setGeometry(int(gx), int(gy), int(gw), int(gh))
            if not self.isVisible():
                self.show()
            self.raise_()
            if not self._pinned:
                # Apply the NSStatusWindowLevel + ignoresMouseEvents treatment once
                # the NSWindow exists (after first show); Qt's transparent-for-mouse
                # alone is not enough on macOS (see macwin docstring).
                from ..macwin import make_always_visible

                make_always_visible(self, click_through=True)
                self._pinned = True
            if not self._timer.isActive():
                self._timer.start(33)  # ~30fps

        def _tick(self) -> None:
            if not self.isVisible() or self._mode is None:
                self._timer.stop()
                return
            self.update()

        def _local(self, x: float, y: float) -> "QPointF":
            ox, oy = self._origin
            return QPointF(x - ox, y - oy)

        # -- painting ------------------------------------------------------ #

        def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override name
            if self._mode is None:
                return
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r, g, b = accent_for(self._risk)
            if self._mode == "reticle" and self._point is not None:
                self._paint_reticle(p, r, g, b)
            elif self._mode == "target" and self._rect is not None:
                self._paint_bracket(p, r, g, b)

        def _paint_reticle(self, p: "QPainter", r: int, g: int, b: int) -> None:
            import time

            assert self._point is not None
            c = self._local(*self._point)

            # Fixed inner crosshair ring: black keyline UNDER a white stroke so it
            # survives any background (the action_highlight self-contrast trick).
            self._stroke_ring(p, c, _RING_R)

            # Breathing risk ring: black keyline under an accent-tinted stroke.
            br = breathe_radius(time.time())
            alpha = int(150 + 90 * breathe_phase(time.time()))
            keyline = QPen(QColor(0, 0, 0, 200), 3.0)
            p.setPen(keyline)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, br, br)
            accent = QPen(QColor(r, g, b, alpha), 1.6)
            p.setPen(accent)
            p.drawEllipse(c, br, br)

            # Center dot: tiny white pip over black keyline so the exact aim point
            # is unambiguous even when the rings breathe.
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 220))
            p.drawEllipse(c, 3.0, 3.0)
            p.setBrush(QColor(255, 255, 255, 255))
            p.drawEllipse(c, 1.6, 1.6)

        def _stroke_ring(self, p: "QPainter", c: "QPointF", radius: float) -> None:
            """White ring over a 1px black under-stroke -> legible on any pixels."""
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(0, 0, 0, 220), 3.4))   # keyline floor
            p.drawEllipse(c, radius, radius)
            p.setPen(QPen(QColor(255, 255, 255, 255), 1.8))  # white cap
            p.drawEllipse(c, radius, radius)

        def _paint_bracket(self, p: "QPainter", r: int, g: int, b: int) -> None:
            import time

            assert self._rect is not None
            ox, oy = self._origin
            rx, ry, rw, rh = self._rect
            local_rect = (rx - ox, ry - oy, rw, rh)
            segs = bracket_segments(local_rect)

            # Pass 1: black under-stroke (slightly fatter) = the keyline floor.
            p.setPen(QPen(QColor(0, 0, 0, 220), 3.6))
            for x1, y1, x2, y2 in segs:
                p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
            # Pass 2: white caps on top -> brackets read over white doc OR video.
            p.setPen(QPen(QColor(255, 255, 255, 255), 2.0))
            for x1, y1, x2, y2 in segs:
                p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

            # A faint, breathing accent edge around the frame ties the bracket to
            # the semantic risk color without hiding the self-contrast brackets.
            alpha = int(70 + 60 * breathe_phase(time.time()))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(r, g, b, alpha), 1.4))
            p.drawRoundedRect(QRectF(rx - ox, ry - oy, rw, rh), 4.0, 4.0)

else:  # HEADLESS: provide a stub so callers get a clear, lazy failure.

    class ReticleWidget:  # type: ignore[no-redef]
        """Stub raised on headless boxes; the real widget needs PyQt6 + a display."""

        def __init__(self, *_, **__):
            raise RuntimeError(
                "ReticleWidget requires PyQt6 + a display (HEADLESS import). "
                "Pure helpers bracket_segments / breathe_radius / accent_for are "
                "available without Qt."
            )

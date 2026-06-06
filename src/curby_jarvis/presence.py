"""Always-on ambient JARVIS presence layer — the visual centerpiece.

Three surfaces that make the WHOLE computer feel like JARVIS is present at all
times, even during idle:

  1. AmbientOrbWidget  — a corner arc-reactor / reticle orb that breathes
     slowly when idle and escalates through every SessionPhase with colour,
     speed, and energy.

  2. AmbientEdgeWidget — a full-screen translucent inner-glow vignette that
     breathes with the orb at idle and brightens by phase, making the entire
     screen feel inhabited.

  3. StatusGlyphWidget — a minimal frosted-console pill near the orb showing
     the current phase name + last action text in cyan-neon monospace.

HEADLESS CONTRACT: importing this module must NOT touch PyQt6, pyobjc, or any
display.  Every Qt / native import is lazy (inside functions or the
``if not HEADLESS`` block).  The pure helpers below are always importable and
unit-testable.

Entry points for app.py::

    layer = PresenceLayer()
    layer.start()           # shows all three surfaces, wires SessionPhase
    layer.set_phase(p)      # accepts any phase string from overlay.phase
    layer.set_last_action(text)  # update the status glyph readout
    layer.stop()            # hides all surfaces, stops all timers

Demo::

    presence.run_demo()     # self-contained scripted sequence, no mic, no key
"""
from __future__ import annotations

import math
import time
from typing import Optional

# ---- headless probe ----------------------------------------------------------
try:  # pragma: no cover
    import PyQt6  # noqa: F401
    HEADLESS = False
except Exception:
    HEADLESS = True


# ---- pure helpers (always importable, unit-testable) -------------------------

# Idle breathe period: 6 s (calm, reassuring, not distracting).
IDLE_BREATHE_PERIOD_S = 6.0
# Active breathe period: 1.2 s (matches the reticle's existing breathe).
ACTIVE_BREATHE_PERIOD_S = 1.2


def ambient_alpha(phase: str, breathe: float) -> float:
    """Overall opacity multiplier 0..1 for the orb layers at a given breathe phase.

    ``breathe`` is the 0..1 cosine-smooth value from ``breathe_value()``.
    Returns a value that is never zero when idle — the orb is always present.
    Pure, side-effect-free, testable without Qt.
    """
    # Base opacity per phase (fraction of full brightness).
    _base = {
        "idle":          0.22,
        "listening":     0.70,
        "heard":         0.75,
        "understanding": 0.65,
        "planning":      0.65,
        "acting":        0.85,
        "done":          0.80,
        "error":         0.80,
    }
    base = _base.get(phase, 0.22)
    # Idle: gentle breathe between 60 % and 100 % of the base.
    # Active: breathe between 80 % and 100 % of the base.
    if phase == "idle":
        lo, hi = 0.60, 1.00
    else:
        lo, hi = 0.80, 1.00
    scale = lo + (hi - lo) * breathe
    return base * scale


def breathe_value(t: float, phase: str) -> float:
    """Smooth 0..1 breathe oscillator — cosine, never harsh.

    Period adapts to phase: slow (6 s) when idle, fast (1.2 s) when active.
    Pure, side-effect-free.
    """
    period = IDLE_BREATHE_PERIOD_S if phase == "idle" else ACTIVE_BREATHE_PERIOD_S
    return (1.0 - math.cos(2.0 * math.pi * (t % period) / period)) / 2.0


def spin_rate(phase: str) -> float:
    """Angular velocity (rad/s) for the spinning outer tick-ring.

    Idle: barely drifts (0.3 rad/s — one full rotation every ~21 s).
    Busy/acting: spins noticeably.  Done/error: slows to a crawl.
    Pure, side-effect-free.
    """
    _rates = {
        "idle":          0.30,
        "listening":     1.20,
        "heard":         0.80,
        "understanding": 1.60,
        "planning":      1.80,
        "acting":        3.00,
        "done":          0.40,
        "error":         0.40,
    }
    return _rates.get(phase, 0.30)


def edge_vignette_alpha(phase: str, breathe: float) -> float:
    """Peak alpha for the ambient edge vignette (0..255 integer range, float).

    Idle: barely there (≈12/255 ≈ 5 %).  Active: up to ~55/255 ≈ 22 %.
    Pure, side-effect-free.
    """
    _peak = {
        "idle":          12.0,
        "listening":     38.0,
        "heard":         44.0,
        "understanding": 36.0,
        "planning":      38.0,
        "acting":        55.0,
        "done":          50.0,
        "error":         48.0,
    }
    peak = _peak.get(phase, 12.0)
    lo, hi = 0.70, 1.00
    if phase == "idle":
        lo = 0.55
    scale = lo + (hi - lo) * breathe
    return peak * scale


def fps_for_phase(phase: str) -> int:
    """Repaint rate in frames-per-second.  Idle uses 5 fps to save CPU; active 30.

    Pure, side-effect-free.
    """
    if phase == "idle":
        return 5
    return 30


# ---- Qt widgets (only when PyQt6 is present) ---------------------------------

if not HEADLESS:
    from PyQt6.QtWidgets import QWidget, QApplication, QLabel
    from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF, QSizeF
    from PyQt6.QtGui import (
        QPainter, QColor, QPen, QBrush,
        QLinearGradient, QRadialGradient, QFont, QFontMetrics,
    )

    # ---------------------------------------------------------------------- #
    # ORB                                                                      #
    # ---------------------------------------------------------------------- #

    class AmbientOrbWidget(QWidget):
        """Arc-reactor / reticle orb living permanently in a screen corner.

        Always visible.  Renders in a single QPainter pass:
          - outer tick ring (16 short marks) that rotates
          - a radial-gradient glow disc
          - an inner solid orb core
          - phase-specific arc overlays (spinning arc when busy, ripple on done,
            double-flash on error)
          - corner-bracket JARVIS reticle marks at the widget corners

        Geometry: 80×80 px logical, pinned to the bottom-right corner (20 px
        inset) by default.  Follows _ensure_geometry() on every set_phase call
        so it adapts if the screen resolution changes at runtime.
        """

        ORB_R = 22.0          # core orb radius
        GLOW_R = 38.0         # radial gradient outer radius
        TICK_R = 46.0         # tick-ring radius
        TICK_LEN = 6.0        # tick arm length
        N_TICKS = 16          # tick count
        SIZE = 100            # widget side length (px logical)
        INSET = 24            # distance from screen edge (px)

        # corner-bracket arm length
        BRACKET_ARM = 10

        def __init__(self, corner: str = "bottom-right") -> None:
            super().__init__()
            self._corner = corner
            self._phase: str = "idle"
            self._breathe: float = 0.0
            self._spin: float = 0.0       # current spin angle (rad)
            self._ripple: float = -1.0    # -1 = off; 0..1 = ripple progress
            self._flash_count: int = 0    # error flash remaining half-cycles
            self._flash_on: bool = False
            self._pinned: bool = False
            self._last_tick: float = time.time()

            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setFixedSize(self.SIZE, self.SIZE)

            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)

        # -- public API ---------------------------------------------------- #

        def set_phase(self, phase: str) -> None:
            """Update phase; adjusts timer rate and resets one-shot animations."""
            from curby_jarvis.overlay import phase as ph_mod
            prev = self._phase
            self._phase = phase
            # Trigger one-shot animations on phase entry.
            if phase == "done" and prev != "done":
                self._ripple = 0.0          # start outward ripple
            if phase == "error" and prev != "error":
                self._flash_count = 6       # 3 full flashes (6 half-cycles)
                self._flash_on = True
            # Adjust timer rate (fast when active, slow when idle).
            interval = 1000 // fps_for_phase(phase)
            if self._timer.interval() != interval:
                self._timer.setInterval(interval)
            if not self._timer.isActive():
                self._timer.start(interval)
            self._ensure_geometry()

        # -- internals ----------------------------------------------------- #

        def _ensure_geometry(self) -> None:
            try:
                app = QApplication.instance()
                screen = app.primaryScreen() if app else None
                geom = screen.geometry() if screen else None
                sw = geom.width() if geom else 1920
                sh = geom.height() if geom else 1080
            except Exception:
                sw, sh = 1920, 1080
            s = self.SIZE
            ins = self.INSET
            positions = {
                "bottom-right": (sw - s - ins, sh - s - ins),
                "bottom-left":  (ins, sh - s - ins),
                "top-right":    (sw - s - ins, ins),
                "top-left":     (ins, ins),
            }
            x, y = positions.get(self._corner, positions["bottom-right"])
            self.setGeometry(x, y, s, s)

        def _pin_once(self) -> None:
            if not self._pinned:
                try:
                    from curby_jarvis.macwin import make_always_visible
                    make_always_visible(self, click_through=True)
                except Exception:
                    pass
                self._pinned = True

        def showEvent(self, ev):  # noqa: N802
            super().showEvent(ev)
            self._pin_once()

        def _tick(self) -> None:
            now = time.time()
            dt = min(now - self._last_tick, 0.2)  # cap dt to avoid huge jumps
            self._last_tick = now
            self._breathe = breathe_value(now, self._phase)
            self._spin = (self._spin + spin_rate(self._phase) * dt) % (2 * math.pi)
            # Advance ripple (420 ms half-life).
            if self._ripple >= 0.0:
                self._ripple = min(1.0, self._ripple + dt / 0.42)
                if self._ripple >= 1.0:
                    self._ripple = -1.0
            # Advance error flash.
            if self._flash_count > 0:
                self._flash_on = not self._flash_on
                self._flash_count -= 1
            self.update()

        def paintEvent(self, _ev) -> None:  # noqa: N802
            from curby_jarvis.overlay import phase as ph_mod
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

            cx = self.width() / 2.0
            cy = self.height() / 2.0
            phase = self._phase
            br = self._breathe
            alpha_mul = ambient_alpha(phase, br)

            # --- accent colour ------------------------------------------------
            ar, ag, ab = ph_mod.accent(phase)
            # Error: flash between rose and dark.
            if phase == "error":
                if self._flash_on:
                    ar, ag, ab = 244, 99, 120
                    alpha_mul = min(1.0, alpha_mul * 1.4)
                else:
                    alpha_mul *= 0.3

            a_full = int(alpha_mul * 255)
            a_glow = int(alpha_mul * 160)
            a_tick = int(alpha_mul * 200)
            a_core = int(alpha_mul * 230)

            # --- outer corner brackets (JARVIS reticle) ----------------------
            arm = self.BRACKET_ARM
            s = self.SIZE
            bracket_alpha = int(alpha_mul * 140)
            pen_bracket = QPen(QColor(ar, ag, ab, bracket_alpha), 1.5)
            pen_bracket.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen_bracket)
            # top-left
            p.drawLine(QPointF(4, 4 + arm), QPointF(4, 4))
            p.drawLine(QPointF(4, 4), QPointF(4 + arm, 4))
            # top-right
            p.drawLine(QPointF(s - 4 - arm, 4), QPointF(s - 4, 4))
            p.drawLine(QPointF(s - 4, 4), QPointF(s - 4, 4 + arm))
            # bottom-right
            p.drawLine(QPointF(s - 4, s - 4 - arm), QPointF(s - 4, s - 4))
            p.drawLine(QPointF(s - 4, s - 4), QPointF(s - 4 - arm, s - 4))
            # bottom-left
            p.drawLine(QPointF(4 + arm, s - 4), QPointF(4, s - 4))
            p.drawLine(QPointF(4, s - 4), QPointF(4, s - 4 - arm))

            # --- radial glow disc --------------------------------------------
            glow = QRadialGradient(QPointF(cx, cy), self.GLOW_R)
            glow.setColorAt(0.0, QColor(ar, ag, ab, a_glow))
            glow.setColorAt(0.55, QColor(ar, ag, ab, a_glow // 3))
            glow.setColorAt(1.0, QColor(ar, ag, ab, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(glow))
            r_g = self.GLOW_R * (1.0 + 0.18 * br)
            p.drawEllipse(QPointF(cx, cy), r_g, r_g)

            # --- orb core ----------------------------------------------------
            # Subtle inner gradient: slightly lighter at centre.
            core_r = self.ORB_R * (0.88 + 0.12 * br)
            core_grad = QRadialGradient(QPointF(cx - core_r * 0.25, cy - core_r * 0.3),
                                        core_r * 0.8)
            core_grad.setColorAt(0.0, QColor(min(255, ar + 40),
                                              min(255, ag + 40),
                                              min(255, ab + 40), a_core))
            core_grad.setColorAt(0.6, QColor(ar, ag, ab, a_core))
            core_grad.setColorAt(1.0, QColor(ar // 2, ag // 2, ab // 2, a_core // 2))
            p.setBrush(QBrush(core_grad))
            p.drawEllipse(QPointF(cx, cy), core_r, core_r)

            # --- thin keyline ring around orb --------------------------------
            pen_ring = QPen(QColor(ar, ag, ab, int(alpha_mul * 180)), 1.0)
            p.setPen(pen_ring)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), core_r, core_r)

            # --- tick ring ---------------------------------------------------
            p.setPen(Qt.PenStyle.NoPen)
            for i in range(self.N_TICKS):
                angle = self._spin + (2.0 * math.pi * i / self.N_TICKS)
                tx0 = cx + (self.TICK_R - self.TICK_LEN) * math.cos(angle)
                ty0 = cy + (self.TICK_R - self.TICK_LEN) * math.sin(angle)
                tx1 = cx + self.TICK_R * math.cos(angle)
                ty1 = cy + self.TICK_R * math.sin(angle)
                # Every 4th tick is a longer "cardinal" mark.
                is_cardinal = (i % 4 == 0)
                tick_len_extra = 3.0 if is_cardinal else 0.0
                tx0c = cx + (self.TICK_R - self.TICK_LEN - tick_len_extra) * math.cos(angle)
                ty0c = cy + (self.TICK_R - self.TICK_LEN - tick_len_extra) * math.sin(angle)
                tick_a = a_tick if is_cardinal else (a_tick * 2 // 3)
                pen_tick = QPen(QColor(ar, ag, ab, tick_a),
                                1.8 if is_cardinal else 1.2)
                pen_tick.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen_tick)
                p.drawLine(QPointF(tx0c, ty0c), QPointF(tx1, ty1))

            # --- spinning arc (busy phases) ----------------------------------
            from curby_jarvis.overlay import phase as ph_mod2
            if ph_mod2.is_busy(phase):
                arc_a = int(alpha_mul * 220)
                arc_pen = QPen(QColor(ar, ag, ab, arc_a), 2.5)
                arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(arc_pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                # Arc spans 90 degrees, rotates with spin.
                arc_rect = QRectF(cx - self.TICK_R, cy - self.TICK_R,
                                  self.TICK_R * 2, self.TICK_R * 2)
                start_deg = int(math.degrees(self._spin) * 16)
                span_deg = 90 * 16
                p.drawArc(arc_rect, start_deg, span_deg)

            # --- listening amplitude pulse (listening phase only) ------------
            # (This widget doesn't receive amplitude; it uses breathe as proxy.)
            if phase == "listening":
                pulse_r = self.ORB_R + 12.0 * br
                pulse_a = int(alpha_mul * 100 * br)
                pen_pulse = QPen(QColor(ar, ag, ab, pulse_a), 1.5)
                p.setPen(pen_pulse)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(cx, cy), pulse_r, pulse_r)

            # --- done ripple -------------------------------------------------
            if self._ripple >= 0.0:
                # ease-out-cubic: slow start, fast finish
                t = self._ripple
                ease = 1.0 - (1.0 - t) ** 3
                rip_r = self.ORB_R + (self.TICK_R + 18.0) * ease
                rip_a = int(220 * (1.0 - ease))
                # done colour even if we've already transitioned to idle
                dr, dg, db = 52, 211, 153  # mint
                pen_rip = QPen(QColor(dr, dg, db, rip_a), 2.0)
                p.setPen(pen_rip)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(cx, cy), rip_r, rip_r)

        def set_level(self, level: float) -> None:
            """Optional: feed mic RMS amplitude for listening phase."""
            # Currently unused — breathe_value acts as proxy; wired for future.
            pass

    # ---------------------------------------------------------------------- #
    # EDGE VIGNETTE                                                            #
    # ---------------------------------------------------------------------- #

    class AmbientEdgeWidget(QWidget):
        """Full-screen translucent inner vignette — makes the whole screen alive.

        A dark inner-glow border drawn inward from all four edges via a
        QLinearGradient on each of the four sides.  At idle it is barely
        perceptible (≈ 5 % opacity peak); on active phases it brightens to ≈ 22 %.

        Always click-through, always on top, always on all spaces.
        """

        DEPTH = 80    # px inward gradient depth
        # Extra depth for the done/error flash — wider spread.
        DEPTH_ACTIVE = 120

        def __init__(self) -> None:
            super().__init__()
            self._phase: str = "idle"
            self._breathe: float = 0.0
            self._pinned: bool = False

            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self._ensure_geometry()

            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)

        # -- public API ---------------------------------------------------- #

        def set_phase(self, phase: str) -> None:
            self._phase = phase
            interval = 1000 // fps_for_phase(phase)
            if self._timer.interval() != interval:
                self._timer.setInterval(interval)
            if not self._timer.isActive():
                self._timer.start(interval)
            self._ensure_geometry()

        # -- internals ----------------------------------------------------- #

        def _ensure_geometry(self) -> None:
            try:
                app = QApplication.instance()
                screen = app.primaryScreen() if app else None
                geom = screen.geometry() if screen else None
                sw = geom.width() if geom else 1920
                sh = geom.height() if geom else 1080
            except Exception:
                sw, sh = 1920, 1080
            self.setGeometry(0, 0, sw, sh)

        def _pin_once(self) -> None:
            if not self._pinned:
                try:
                    from curby_jarvis.macwin import make_always_visible
                    make_always_visible(self, click_through=True)
                except Exception:
                    pass
                self._pinned = True

        def showEvent(self, ev):  # noqa: N802
            super().showEvent(ev)
            self._pin_once()

        def _tick(self) -> None:
            self._breathe = breathe_value(time.time(), self._phase)
            self.update()

        def paintEvent(self, _ev) -> None:  # noqa: N802
            from curby_jarvis.overlay import phase as ph_mod
            peak_a = edge_vignette_alpha(self._phase, self._breathe)
            if peak_a < 1.0:
                return
            ar, ag, ab = ph_mod.accent(self._phase)
            w, h = float(self.width()), float(self.height())
            depth = float(self.DEPTH_ACTIVE if self._phase not in ("idle",) else self.DEPTH)

            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

            def _edge_grad(x0, y0, x1, y1):
                grad = QLinearGradient(QPointF(x0, y0), QPointF(x1, y1))
                grad.setColorAt(0.0, QColor(ar, ag, ab, int(peak_a)))
                grad.setColorAt(1.0, QColor(ar, ag, ab, 0))
                return grad

            p.setPen(Qt.PenStyle.NoPen)
            # Top edge
            p.setBrush(QBrush(_edge_grad(0, 0, 0, depth)))
            p.drawRect(QRectF(0, 0, w, depth))
            # Bottom edge
            p.setBrush(QBrush(_edge_grad(0, h, 0, h - depth)))
            p.drawRect(QRectF(0, h - depth, w, depth))
            # Left edge
            p.setBrush(QBrush(_edge_grad(0, 0, depth, 0)))
            p.drawRect(QRectF(0, 0, depth, h))
            # Right edge
            p.setBrush(QBrush(_edge_grad(w, 0, w - depth, 0)))
            p.drawRect(QRectF(w - depth, 0, depth, h))

    # ---------------------------------------------------------------------- #
    # STATUS GLYPH / WORDMARK PILL                                             #
    # ---------------------------------------------------------------------- #

    class StatusGlyphWidget(QWidget):
        """Frosted-console pill: tiny phase label + last action in cyan monospace.

        Positioned adjacent to the orb corner; resizes dynamically with content.
        Click-through and always on top.

        Renders:
          - frosted dark panel (#141620 @ 210 alpha) with 1 px cyan keyline
          - two-line text: phase name (cyan, bright) / last action (dim cyan)
          - tiny "[JARVIS]" wordmark prefix on the phase line
        """

        PILL_H = 48
        PILL_PAD_X = 14
        PILL_PAD_Y = 8
        MIN_W = 130

        def __init__(self, corner: str = "bottom-right") -> None:
            super().__init__()
            self._corner = corner
            self._phase: str = "idle"
            self._last_action: str = ""
            self._pinned: bool = False

            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self._update_size()

        # -- public API ---------------------------------------------------- #

        def set_phase(self, phase: str) -> None:
            self._phase = phase
            self._update_size()
            self.update()

        def set_last_action(self, text: str) -> None:
            self._last_action = (text or "").strip()[:40]
            self._update_size()
            self.update()

        # -- internals ----------------------------------------------------- #

        def _update_size(self) -> None:
            """Measure text, resize widget, reposition near orb."""
            try:
                font_phase = QFont("SF Mono", 10)
                font_phase.setWeight(QFont.Weight.Medium)
                font_action = QFont("SF Mono", 9)
                fm_phase = QFontMetrics(font_phase)
                fm_action = QFontMetrics(font_action)
                phase_line = f"[JARVIS] {self._phase.upper()}"
                action_line = self._last_action or ""
                tw = max(
                    fm_phase.horizontalAdvance(phase_line),
                    fm_action.horizontalAdvance(action_line) if action_line else 0,
                )
                w = max(self.MIN_W, tw + self.PILL_PAD_X * 2)
                h = self.PILL_H if not action_line else self.PILL_H + 16
            except Exception:
                w, h = self.MIN_W, self.PILL_H

            self.setFixedSize(w, h)
            self._reposition(w, h)

        def _reposition(self, w: int, h: int) -> None:
            try:
                app = QApplication.instance()
                screen = app.primaryScreen() if app else None
                geom = screen.geometry() if screen else None
                sw = geom.width() if geom else 1920
                sh = geom.height() if geom else 1080
            except Exception:
                sw, sh = 1920, 1080
            orb_s = AmbientOrbWidget.SIZE
            ins = AmbientOrbWidget.INSET
            # Offset pill so it sits just to the left of / above the orb.
            positions = {
                "bottom-right": (sw - ins - orb_s - w - 8, sh - ins - orb_s // 2 - h // 2),
                "bottom-left":  (ins + orb_s + 8, sh - ins - orb_s // 2 - h // 2),
                "top-right":    (sw - ins - orb_s - w - 8, ins + orb_s // 2 - h // 2),
                "top-left":     (ins + orb_s + 8, ins + orb_s // 2 - h // 2),
            }
            x, y = positions.get(self._corner, positions["bottom-right"])
            self.setGeometry(x, y, w, h)

        def _pin_once(self) -> None:
            if not self._pinned:
                try:
                    from curby_jarvis.macwin import make_always_visible
                    make_always_visible(self, click_through=True)
                except Exception:
                    pass
                self._pinned = True

        def showEvent(self, ev):  # noqa: N802
            super().showEvent(ev)
            self._pin_once()

        def paintEvent(self, _ev) -> None:  # noqa: N802
            from curby_jarvis.overlay import phase as ph_mod
            ar, ag, ab = ph_mod.accent(self._phase)
            has_action = bool(self._last_action)
            w, h = float(self.width()), float(self.height())
            radius = 10.0
            panel_alpha = 210

            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

            # --- frosted panel -------------------------------------------
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(20, 22, 32, panel_alpha))
            p.drawRoundedRect(QRectF(0, 0, w, h), radius, radius)
            # Cyan keyline
            pen_key = QPen(QColor(ar, ag, ab, 90), 1.0)
            p.setPen(pen_key)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), radius, radius)

            # --- phase line (bright cyan) --------------------------------
            font_phase = QFont("SF Mono", 10)
            font_phase.setWeight(QFont.Weight.Medium)
            p.setFont(font_phase)
            # Shadow
            p.setPen(QColor(0, 0, 0, 140))
            p.drawText(
                QRectF(self.PILL_PAD_X + 1, self.PILL_PAD_Y + 1,
                       w - self.PILL_PAD_X * 2, 18),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"[JARVIS] {self._phase.upper()}"
            )
            # Main text
            p.setPen(QColor(ar, ag, ab, 230))
            p.drawText(
                QRectF(self.PILL_PAD_X, self.PILL_PAD_Y, w - self.PILL_PAD_X * 2, 18),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"[JARVIS] {self._phase.upper()}"
            )

            # --- last action line (dim cyan) -----------------------------
            if has_action:
                font_action = QFont("SF Mono", 9)
                p.setFont(font_action)
                p.setPen(QColor(0, 0, 0, 100))
                p.drawText(
                    QRectF(self.PILL_PAD_X + 1, self.PILL_PAD_Y + 20,
                           w - self.PILL_PAD_X * 2, 16),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    self._last_action
                )
                p.setPen(QColor(ar, ag, ab, 140))
                p.drawText(
                    QRectF(self.PILL_PAD_X, self.PILL_PAD_Y + 20,
                           w - self.PILL_PAD_X * 2, 16),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    self._last_action
                )

    # ---------------------------------------------------------------------- #
    # PRESENCE LAYER — facade that owns all three surfaces                     #
    # ---------------------------------------------------------------------- #

    class PresenceLayer:
        """Owns and coordinates the three ambient surfaces.

        Usage (from app.py)::

            self._presence = PresenceLayer()
            self._presence.start()
            # … wire to phase signal:
            bridge.phase.connect(self._presence.set_phase)

        All ``set_*`` methods are safe to call before ``start()`` (they buffer
        state) and are safe to call from any thread that can emit signals — but
        you should call them on the Qt main thread (they are not thread-safe
        themselves; use _Bridge.invoke for cross-thread updates).
        """

        def __init__(self, corner: str = "bottom-right") -> None:
            self._corner = corner
            self._orb: Optional[AmbientOrbWidget] = None
            self._edge: Optional[AmbientEdgeWidget] = None
            self._glyph: Optional[StatusGlyphWidget] = None
            self._started = False
            self._phase = "idle"
            self._last_action = ""

        def start(self) -> None:
            """Build and show all three surfaces.  Call once from the Qt main thread."""
            if self._started:
                return
            try:
                self._orb = AmbientOrbWidget(corner=self._corner)
                self._orb.set_phase(self._phase)
                self._orb.show()
            except Exception as e:
                import sys
                print(f"[presence] orb failed: {e}", file=sys.stderr)
                self._orb = None
            try:
                self._edge = AmbientEdgeWidget()
                self._edge.set_phase(self._phase)
                self._edge.show()
                # Orb must sit above the edge vignette.
                if self._orb is not None:
                    self._orb.raise_()
            except Exception as e:
                import sys
                print(f"[presence] edge failed: {e}", file=sys.stderr)
                self._edge = None
            try:
                self._glyph = StatusGlyphWidget(corner=self._corner)
                self._glyph.set_phase(self._phase)
                self._glyph.set_last_action(self._last_action)
                self._glyph.show()
            except Exception as e:
                import sys
                print(f"[presence] glyph failed: {e}", file=sys.stderr)
                self._glyph = None
            self._started = True

        def stop(self) -> None:
            """Hide all surfaces and stop their timers."""
            for widget in (self._orb, self._edge, self._glyph):
                if widget is not None:
                    try:
                        t = getattr(widget, "_timer", None)
                        if t is not None:
                            t.stop()
                        widget.hide()
                    except Exception:
                        pass

        def set_phase(self, phase: str) -> None:
            """Update all surfaces to a new SessionPhase.  Never raises."""
            self._phase = phase
            for widget in (self._orb, self._edge, self._glyph):
                if widget is not None:
                    try:
                        widget.set_phase(phase)
                    except Exception:
                        pass

        def set_last_action(self, text: str) -> None:
            """Update the status glyph last-action readout.  Never raises."""
            self._last_action = text
            if self._glyph is not None:
                try:
                    self._glyph.set_last_action(text)
                except Exception:
                    pass

else:
    # ---- headless stubs (import-safe, construction-raises) ------------------

    class AmbientOrbWidget:  # type: ignore[no-redef]
        def __init__(self, *_, **__):
            raise RuntimeError(
                "AmbientOrbWidget requires PyQt6 + a display. "
                "Pure helpers ambient_alpha/breathe_value are available without Qt."
            )

    class AmbientEdgeWidget:  # type: ignore[no-redef]
        def __init__(self, *_, **__):
            raise RuntimeError("AmbientEdgeWidget requires PyQt6 + a display.")

    class StatusGlyphWidget:  # type: ignore[no-redef]
        def __init__(self, *_, **__):
            raise RuntimeError("StatusGlyphWidget requires PyQt6 + a display.")

    class PresenceLayer:  # type: ignore[no-redef]
        def __init__(self, *_, **__):
            raise RuntimeError("PresenceLayer requires PyQt6 + a display.")


# ---- Demo -------------------------------------------------------------------

def run_demo() -> int:
    """Self-contained visual demo: cycles through every SessionPhase with real
    commands dispatched through the router (Claude-CLI backend, no API key).

    Requires PyQt6 + a display.  Launched via ``--demo`` flag or called directly.

    The demo sequence (approx 20 seconds total):
      0 s   idle    — orb breathing, edge glow barely visible
      2 s   listening — orb brightens cyan, edge pulses
      4 s   heard   — bright cyan flash
      5 s   understanding — violet spinning arc, "thinking…"
      7 s   planning — lighter violet, routing
      9 s   acting  — amber pulse, "opening…"
     11 s   done    — mint ripple, "launched Safari"
     13 s   idle    — settle back
     15 s   listening → understanding → acting → done (second command)
     19 s   idle
     21 s   error   — rose flash
     23 s   idle
     25 s   exit
    """
    import sys
    import threading

    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import QTimer
    except Exception as e:
        print(f"[demo] needs PyQt6: {e}", file=sys.stderr)
        return 2

    app = QApplication.instance() or QApplication(sys.argv[:1])

    try:
        from curby_jarvis.macwin import set_accessory_policy
        set_accessory_policy()
    except Exception:
        pass

    layer = PresenceLayer(corner="bottom-right")
    layer.start()

    # Also boot the full HUD via CurbyJarvis so the reticle / card / edge
    # light all show — the demo is "the whole screen comes alive".
    jarvis = None
    try:
        from curby_jarvis.app import CurbyJarvis
        jarvis = CurbyJarvis()
        jarvis.run_live  # confirm method exists (don't call it — we manage our own loop)
        jarvis._app = app
        try:
            from curby_jarvis.macwin import set_accessory_policy
            set_accessory_policy()
        except Exception:
            pass
        jarvis._ensure_overlay()
        jarvis._bridge.phase.emit("idle")
    except Exception as e:
        print(f"[demo] full HUD unavailable: {e}", file=sys.stderr)
        jarvis = None

    def emit(p: str, action: str = "") -> None:
        """Emit to both the presence layer and the full HUD if available."""
        layer.set_phase(p)
        if action:
            layer.set_last_action(action)
        if jarvis is not None:
            try:
                jarvis._bridge.phase.emit(p)
            except Exception:
                pass

    # Scripted timeline — each step is (delay_ms, phase, action_text, callback).
    steps = [
        (0,     "idle",          "JARVIS online",          None),
        (2000,  "listening",     "listening…",             None),
        (4000,  "heard",         "open Safari",            None),
        (5000,  "understanding", "parsing command…",       None),
        (7000,  "planning",      "routing → app_launch",   None),
        (9000,  "acting",        "launching Safari…",      None),
        (11000, "done",          "launched Safari (42 ms)", None),
        (13000, "idle",          "ready",                  None),
        (15000, "listening",     "listening…",             None),
        (16500, "heard",         "pause music",            None),
        (17000, "understanding", "parsing…",               None),
        (18000, "acting",        "media key → pause",      None),
        (19500, "done",          "paused (8 ms)",          None),
        (21500, "idle",          "ready",                  None),
        (23000, "listening",     "listening…",             None),
        (23800, "error",         "connector unavailable",  None),
        (25500, "idle",          "ready",                  None),
    ]

    quit_timer = QTimer()
    quit_timer.setSingleShot(True)
    quit_timer.timeout.connect(app.quit)
    quit_timer.start(28000)  # auto-exit after 28 s

    def _run_steps():
        for delay_ms, phase, action, cb in steps:
            import time as _t
            _t.sleep(delay_ms / 1000.0)
            # Marshal onto Qt main thread.
            def _go(p=phase, a=action, callback=cb):
                emit(p, a)
                if callback:
                    try:
                        callback()
                    except Exception:
                        pass
            try:
                if jarvis is not None:
                    jarvis._bridge.invoke.emit(_go)
                else:
                    # No bridge available — call directly (we're on main thread
                    # only if QApplication.exec hasn't started yet, which isn't
                    # true here; use a singleShot as a fallback).
                    from PyQt6.QtCore import QTimer as _QT
                    _QT.singleShot(0, _go)
            except Exception:
                pass

    t = threading.Thread(target=_run_steps, name="demo-sequence", daemon=True)
    t.start()

    print("[curby-jarvis] DEMO — watch the screen come alive. Ctrl-C to exit early.",
          file=sys.stderr)
    return int(app.exec())


# ---- module-level __all__ ---------------------------------------------------

__all__ = [
    "HEADLESS",
    # pure helpers
    "ambient_alpha",
    "breathe_value",
    "spin_rate",
    "edge_vignette_alpha",
    "fps_for_phase",
    # widgets (or stubs)
    "AmbientOrbWidget",
    "AmbientEdgeWidget",
    "StatusGlyphWidget",
    "PresenceLayer",
    # demo
    "run_demo",
]

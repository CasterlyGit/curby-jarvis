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

Extended (UI-02/04/05/09/10/15): the reticle became a living orb — phase-driven
paint modes (idle breathe / listening amplitude-reactive / thinking spin / acting
pulse / done ripple / error flash), lock-on targeting brackets, pinch-to-confirm
arc, ghost drag rect, and chain-progress ring all share the single 30fps _timer.
"""
from __future__ import annotations

import math
import time
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

# ---- orb phase constants ----------------------------------------------------
# Phase-string → paint mode mapping.  Unknown strings fall back to "idle".
_PHASE_MODES = {
    "idle": "idle",
    "listening": "listening",
    "heard": "heard",
    "understanding": "thinking",
    "planning": "thinking",
    "acting": "acting",
    "done": "done",
    "error": "error",
}

# Arc segment colors for chain-progress ring: violet neutral
_CHAIN_NEUTRAL = (0xB0, 0x8E, 0xFF)
_CHAIN_ACTIVE  = (0x34, 0xD3, 0x99)  # mint = resolved
_CHAIN_RING_R_OFFSET = 8.0            # px outside the breathe ring max

# Targeting bracket neutral color before lock (violet)
_LOCK_NEUTRAL = (0xB0, 0x8E, 0xFF)
# lock 0→1 cross-fades violet → amber → risk color
_LOCK_MID_COLOR = (0xF0, 0xA5, 0x00)   # amber

# Ghost rect translucency
_GHOST_ALPHA = 90  # ~35% of 255


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

        Extended (UI-02/04/05/09/10/15): the orb is now phase-driven — idle
        breathes, listening scales by amplitude, thinking/planning/understanding
        spin an arc, acting fast-pulses, done fires a single outward ripple,
        error double-flashes rose.  Lock-on brackets animate inward; a confirm
        arc fills 0..1 around the orb; a ghost rect tracks drag targets; a
        segmented chain ring records router walk order.
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

            # --- orb phase state (UI-02) ---
            self._orb_phase: str = "idle"   # current phase string
            self._spin_phase: float = 0.0   # rotating arc angle accumulator (radians)

            # --- amplitude / level (UI-04) ---
            self._amplitude: float = 0.0    # smoothed 0..1 mic level

            # --- lock-on targeting (UI-05) ---
            self._lock: float = 0.0         # 0 = wide neutral, 1 = locked-on

            # --- pinch-to-confirm progress (UI-09) ---
            self._confirm: float = 0.0      # 0..1 fill arc

            # --- ghost drag rect (UI-10) ---
            # Stored as screen-absolute (x, y, w, h); None when not active.
            self._ghost: Optional[tuple] = None

            # --- done ripple state (internal) ---
            # _ripple_t ∈ [0,1]: 0 = just fired, 1 = fully faded; None = inactive.
            self._ripple_t: Optional[float] = None
            self._ripple_start: float = 0.0

            # --- error flash state (internal) ---
            # _error_flash_count: remaining half-cycles (2 per flash, 2 flashes = 4)
            self._error_flash_count: int = 0
            self._error_flash_t: float = 0.0

            # --- chain-progress ring (UI-15) ---
            # list of (connector_name, resolved:bool) in walk order
            self._chain: list[tuple[str, bool]] = []

        # -- public API (new, UI-02/04/05/09/10/15) ------------------------ #

        def set_phase(self, phase_str: str) -> None:
            """Drive the orb into a new animation mode (UI-02).

            idle → slow breathe; listening → amplitude-reactive scale;
            understanding/planning/acting → spinning arc or fast pulse;
            done → single outward ripple (auto-fades via _timer);
            error → two rose flashes.
            """
            from curby_jarvis.overlay import phase as _phase

            prev = self._orb_phase
            self._orb_phase = phase_str

            # Fire one-shot effects on entry.
            if phase_str == "done" and prev != "done":
                self._ripple_t = 0.0
                self._ripple_start = time.monotonic()
                self._error_flash_count = 0
            elif phase_str == "error" and prev != "error":
                self._error_flash_count = 4   # 2 flashes × 2 half-cycles
                self._error_flash_t = time.monotonic()
                self._ripple_t = None

            if self.isVisible():
                self.update()

        def set_level(self, level: float) -> None:
            """Store smoothed amplitude 0..1 for listening scale/alpha (UI-04).

            The caller (app.py _Bridge level signal) pushes this at ~30Hz.
            Exponential smoothing is kept in the caller; we just store and repaint.
            """
            from curby_jarvis.overlay import motion as _motion
            # Gentle smoothing so a single spike doesn't jerk the ring.
            alpha = 0.35
            self._amplitude = _motion.clamp(
                (1.0 - alpha) * self._amplitude + alpha * level
            )
            if self.isVisible():
                self.update()

        def set_lock_phase(self, lock: float) -> None:
            """Animate targeting brackets from wide-neutral to locked-on (UI-05).

            lock=0 → full bracket width in neutral violet;
            lock=1 → brackets closed (arms converged) in risk accent color.
            In between: lerp arm length and cross-fade violet→amber→risk color.
            """
            from curby_jarvis.overlay import motion as _motion
            self._lock = _motion.clamp(lock)
            if self.isVisible():
                self.update()

        def set_confirm_progress(self, progress: float) -> None:
            """Fill the pinch-to-confirm arc around the orb (UI-09).

            progress 0..1 sweeps a full 360° arc; 0 = invisible.
            """
            from curby_jarvis.overlay import motion as _motion
            self._confirm = _motion.clamp(progress)
            if self.isVisible():
                self.update()

        def show_ghost(self, rect) -> None:
            """Show a translucent ghost rect tracking a pointer drag target (UI-10).

            rect is screen-absolute (x, y, w, h).  The ghost is drawn at ~35%
            alpha with a 1px accent outline.
            """
            x, y, w, h = (float(v) for v in rect)
            self._ghost = (x, y, w, h)
            if self.isVisible():
                self.update()

        def move_ghost(self, x: float, y: float) -> None:
            """Reposition the ghost rect top-left (UI-10); preserves width/height."""
            if self._ghost is not None:
                _, _, w, h = self._ghost
                self._ghost = (float(x), float(y), w, h)
                if self.isVisible():
                    self.update()

        def drop_ghost(self) -> None:
            """Remove the ghost drag rect (UI-10)."""
            self._ghost = None
            if self.isVisible():
                self.update()

        def show_chain_progress(self, connector_name: str, resolved: bool) -> None:
            """Append a connector step to the segmented chain ring (UI-15).

            The ring sits just outside the breathe ring; each segment lights up in
            cost order as the router walks the chain.  Call reset_chain() before a
            new utterance.
            """
            self._chain.append((connector_name, resolved))
            if self.isVisible():
                self.update()

        def reset_chain(self) -> None:
            """Clear the chain-progress ring for a fresh utterance."""
            self._chain = []
            if self.isVisible():
                self.update()

        # -- existing public API ------------------------------------------- #

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
            # Advance spin for thinking/planning/acting modes (~30fps = 33ms/tick)
            paint_mode = _PHASE_MODES.get(self._orb_phase, "idle")
            if paint_mode in ("thinking", "acting"):
                speed = 2.5 if paint_mode == "acting" else 1.2   # rad/s
                self._spin_phase += speed * 0.033
                if self._spin_phase > 2 * math.pi:
                    self._spin_phase -= 2 * math.pi

            # Advance ripple (done animation, 420ms total)
            if self._ripple_t is not None:
                elapsed = time.monotonic() - self._ripple_start
                duration = 0.420  # seconds = motion.DURATIONS['done_ripple'] ms / 1000
                self._ripple_t = min(elapsed / duration, 1.0)
                if self._ripple_t >= 1.0:
                    self._ripple_t = None  # auto-fade complete

            # Advance error flash (each half-cycle ~80ms)
            if self._error_flash_count > 0:
                if time.monotonic() - self._error_flash_t > 0.08:
                    self._error_flash_count -= 1
                    self._error_flash_t = time.monotonic()

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

            # Ghost drag rect is drawn regardless of reticle/target mode.
            if self._ghost is not None:
                self._paint_ghost(p, r, g, b)

        def _paint_reticle(self, p: "QPainter", r: int, g: int, b: int) -> None:
            from curby_jarvis.overlay import phase as _phase_mod
            from curby_jarvis.overlay import motion as _motion

            assert self._point is not None
            c = self._local(*self._point)

            now = time.time()
            paint_mode = _PHASE_MODES.get(self._orb_phase, "idle")

            # ---- orb radius & alpha by mode (UI-02) ----
            if paint_mode == "listening":
                # Amplitude-reactive scale: 1.0 → 1.35x (UI-04)
                scale = 1.0 + 0.35 * self._amplitude
                br = breathe_radius(now) * scale
                alpha = int(160 + 80 * self._amplitude)
            elif paint_mode in ("thinking",):
                # Stable radius; spinning arc painted below
                br = breathe_radius(now)
                alpha = 180
            elif paint_mode == "acting":
                # Fast pulse: use a faster breathe period
                fast_period = 0.4
                br = _BREATHE_MIN_R + (_BREATHE_MAX_R - _BREATHE_MIN_R) * breathe_phase(now, fast_period)
                alpha = 200
            elif paint_mode == "done":
                # Base ring dims; ripple expands outward
                br = breathe_radius(now)
                alpha = 120
            elif paint_mode == "error":
                br = breathe_radius(now)
                # Flash rose on odd half-cycles
                if self._error_flash_count % 2 == 1:
                    r, g, b = 0xFF, 0x5B, 0x8A  # rose
                    alpha = 240
                else:
                    alpha = 80
            else:
                # idle/heard: normal slow breathe
                br = breathe_radius(now)
                alpha = int(150 + 90 * breathe_phase(now))

            # ---- phase accent color (UI-02) ----
            if paint_mode not in ("error",):
                # Prefer phase accent over risk color while an orb-phase is active
                pr, pg, pb = _phase_mod.accent(self._orb_phase)
                # Blend with risk when lock>0 (risk bleeds in as target locks)
                if self._lock > 0.0 and self._risk:
                    rr, rg, rb = accent_for(self._risk)
                    pr, pg, pb = _motion.lerp_rgb((pr, pg, pb), (rr, rg, rb), self._lock)
                r, g, b = pr, pg, pb

            # Fixed inner crosshair ring
            self._stroke_ring(p, c, _RING_R)

            # Breathing / phase-reactive ring
            keyline = QPen(QColor(0, 0, 0, 200), 3.0)
            p.setPen(keyline)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(c, br, br)
            accent_pen = QPen(QColor(r, g, b, alpha), 1.6)
            p.setPen(accent_pen)
            p.drawEllipse(c, br, br)

            # ---- spinning arc for thinking/acting modes (UI-02) ----
            if paint_mode in ("thinking", "acting"):
                self._paint_spin_arc(p, c, br + 4.0, r, g, b)

            # ---- done ripple (UI-02) ----
            if self._ripple_t is not None:
                ease_t = _motion.ease_out_cubic(self._ripple_t)
                rip_r = br + 20.0 * ease_t
                rip_alpha = int(200 * (1.0 - ease_t))
                p.setPen(QPen(QColor(r, g, b, rip_alpha), 1.5))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(c, rip_r, rip_r)

            # ---- lock-on brackets (UI-05) ----
            self._paint_lock_brackets(p, c, r, g, b)

            # ---- confirm arc (UI-09) ----
            if self._confirm > 0.0:
                self._paint_confirm_arc(p, c, br + 7.0, r, g, b)

            # ---- chain ring (UI-15) ----
            if self._chain:
                self._paint_chain_ring(p, c, br + _CHAIN_RING_R_OFFSET)

            # Center dot
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 220))
            p.drawEllipse(c, 3.0, 3.0)
            p.setBrush(QColor(255, 255, 255, 255))
            p.drawEllipse(c, 1.6, 1.6)

        def _paint_spin_arc(
            self, p: "QPainter", c: "QPointF", radius: float,
            r: int, g: int, b: int
        ) -> None:
            """Rotating arc segment (120° sweep) for thinking/acting modes."""
            from PyQt6.QtCore import QRectF as _QRectF
            span = 120 * 16   # Qt arc uses 1/16-degree units
            # _spin_phase in radians; Qt uses degrees * 16 starting from 3-o'clock
            start_deg = int(math.degrees(self._spin_phase) * 16) % (360 * 16)
            rect = _QRectF(c.x() - radius, c.y() - radius, 2 * radius, 2 * radius)
            p.setPen(QPen(QColor(r, g, b, 210), 2.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(rect, start_deg, span)

        def _paint_lock_brackets(
            self, p: "QPainter", c: "QPointF", r: int, g: int, b: int
        ) -> None:
            """Targeting brackets: wide-neutral → inward converge as lock 0→1 (UI-05)."""
            from curby_jarvis.overlay import motion as _motion

            lock = self._lock
            # Bracket box shrinks as lock increases; fully locked = small tight ring
            full_half = _BREATHE_MAX_R + 14.0
            locked_half = _RING_R + 4.0
            half = _motion.lerp(full_half, locked_half, lock)

            # Color: violet → amber (0→0.5) → risk (0.5→1)
            risk_rgb = accent_for(self._risk)
            if lock <= 0.5:
                t = lock * 2.0
                lr, lg, lb = _motion.lerp_rgb(_LOCK_NEUTRAL, _LOCK_MID_COLOR, t)
            else:
                t = (lock - 0.5) * 2.0
                lr, lg, lb = _motion.lerp_rgb(_LOCK_MID_COLOR, risk_rgb, t)

            alpha = int(140 + 80 * lock)
            ox, oy = self._origin
            # Use bracket_segments in widget-local space around the centre point
            bx = c.x() - half
            by = c.y() - half
            segs = bracket_segments((bx, by, half * 2, half * 2))

            p.setPen(QPen(QColor(0, 0, 0, 180), 2.8))
            for x1, y1, x2, y2 in segs:
                p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
            p.setPen(QPen(QColor(lr, lg, lb, alpha), 1.8))
            for x1, y1, x2, y2 in segs:
                p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        def _paint_confirm_arc(
            self, p: "QPainter", c: "QPointF", radius: float,
            r: int, g: int, b: int
        ) -> None:
            """Sweep arc 0..360 as confirm progress 0..1 (UI-09)."""
            from PyQt6.QtCore import QRectF as _QRectF
            span = int(self._confirm * 360 * 16)
            start = 90 * 16   # 12-o'clock
            rect = _QRectF(c.x() - radius, c.y() - radius, 2 * radius, 2 * radius)
            # Keyline under
            p.setPen(QPen(QColor(0, 0, 0, 180), 3.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(rect, start, -span)
            # Amber fill arc (confirm color)
            p.setPen(QPen(QColor(0xF0, 0xA5, 0x00, 220), 2.0))
            p.drawArc(rect, start, -span)

        def _paint_chain_ring(
            self, p: "QPainter", c: "QPointF", ring_r: float
        ) -> None:
            """Segmented arc ring in connector cost order (UI-15)."""
            from PyQt6.QtCore import QRectF as _QRectF

            n = len(self._chain)
            if n == 0:
                return
            gap = 8.0   # degrees between segments
            per_seg = (360.0 - gap * n) / n
            rect = _QRectF(c.x() - ring_r, c.y() - ring_r, 2 * ring_r, 2 * ring_r)

            for i, (name, resolved) in enumerate(self._chain):
                start_deg = int((-90 + i * (per_seg + gap)) * 16)
                span_deg = int(per_seg * 16)
                if resolved:
                    sr, sg, sb = _CHAIN_ACTIVE
                    alpha = 220
                else:
                    sr, sg, sb = _CHAIN_NEUTRAL
                    alpha = 120
                p.setPen(QPen(QColor(sr, sg, sb, alpha), 2.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawArc(rect, start_deg, span_deg)

        def _paint_ghost(self, p: "QPainter", r: int, g: int, b: int) -> None:
            """Ghost drag rect: translucent fill + 1px accent outline (UI-10)."""
            assert self._ghost is not None
            gx, gy, gw, gh = self._ghost
            ox, oy = self._origin
            lx, ly = gx - ox, gy - oy
            rect = QRectF(lx, ly, gw, gh)
            # Translucent fill ~35%
            p.setBrush(QColor(r, g, b, _GHOST_ALPHA))
            p.setPen(QPen(QColor(r, g, b, 200), 1.0))
            p.drawRoundedRect(rect, 3.0, 3.0)

        def _stroke_ring(self, p: "QPainter", c: "QPointF", radius: float) -> None:
            """White ring over a 1px black under-stroke -> legible on any pixels."""
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(0, 0, 0, 220), 3.4))   # keyline floor
            p.drawEllipse(c, radius, radius)
            p.setPen(QPen(QColor(255, 255, 255, 255), 1.8))  # white cap
            p.drawEllipse(c, radius, radius)

        def _paint_bracket(self, p: "QPainter", r: int, g: int, b: int) -> None:
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

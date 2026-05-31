"""Frosted Console preview card — the gated confirm/audit surface.

The card is a deep frosted-glass panel (bg #1a1d27 @ alpha 244) so its TEXT never
depends on the pixels behind it — unlike the reticle/bracket layer (overlay/
reticle.py), the card does NOT use the action_highlight self-contrast trick; the
opaque frost is the contrast guarantee. A 1px #232631 border and a soft purple
(#b08eff) outer glow sit under a semantic risk edge: launch=cyan, reversible=mint,
irreversible=rose, ambiguous=amber (intent.RISK_*).

It renders a PreviewCard (intent.PreviewCard): title (the verb line), gloss (the
resolved target line), literal (URL / AppleScript / coord, for audit), and a
mechanism+latency badge. Confirm/Cancel buttons appear ONLY when an on_confirm
callback is supplied; with no callback the card is informational and auto-dismisses.

HEADLESS CONTRACT: importing this module must not import PyQt6. `HEADLESS` is True
when PyQt6 is unavailable; the widget class only exists off that path. The pure
helper `risk_color(risk) -> (r, g, b)` is unit-testable with no QApplication and
is the single source of the locked semantic hexes.

The pure helper `format_latency_chip(latency_ms, grade_str) -> str` is also
unit-testable headlessly; it formats the 'DID IT IN N ms' chip text.

Threading: the widget never spawns threads. Per the overlay contract, the app owns
the single _Bridge and marshals confirm/cancel onto the Qt main thread; the
callbacks passed here are already main-thread-safe by the time show_card runs.

UI-06 (show_status): transient, button-less phase label + partial content + animated
top-edge progress bar; replaced in-place by show_card.

UI-07 (latency chip): show_done(latency_dict) or show_card(latency=dict) drives an
animated 'DID IT IN N ms' count-up chip, color-graded green/amber/rose.

UI-11 (undo toast): show_undo_toast(label, seconds, on_undo) shows a countdown
pill with an Undo chip; used by reversible actions post-completion.

UI-14 (micro-animations): entry uses QSequentialAnimationGroup: 80ms opacity
OutCubic + 120ms slide-in via _y_offset + 60ms accent brighten. Risk color
cross-fades over 40ms on risk change.

UI-12 (adaptive ink): PreviewCardWidget delegates panel style to AdaptiveInk at 2fps.
"""
from __future__ import annotations

from typing import Callable, Optional

from ..intent import (
    RISK_AMBIGUOUS,
    RISK_IRREVERSIBLE,
    RISK_LAUNCH,
    RISK_REVERSIBLE,
    PreviewCard,
)

# ---- headless probe ---------------------------------------------------------
try:  # pragma: no cover - trivial availability flag
    import PyQt6  # noqa: F401

    HEADLESS = False
except Exception:  # PyQt6 absent -> module still imports; widget unusable
    HEADLESS = True


# ---- locked Frosted Console palette -----------------------------------------
_RISK_RGB: dict[str, tuple[int, int, int]] = {
    RISK_LAUNCH: (0x00, 0xE4, 0xFF),
    RISK_REVERSIBLE: (0x2E, 0xE5, 0x9D),
    RISK_IRREVERSIBLE: (0xFF, 0x5B, 0x8A),
    RISK_AMBIGUOUS: (0xF0, 0xA5, 0x00),
}
_PURPLE = (0xB0, 0x8E, 0xFF)

_PANEL_RGB = (0x1A, 0x1D, 0x27)
_PANEL_ALPHA = 244
_BORDER_RGB = (0x23, 0x26, 0x31)
_CARD_RADIUS = 12

DEFAULT_AUTO_DISMISS_MS = 2600

# Latency chip grade -> RGB
_CHIP_COLORS: dict[str, tuple[int, int, int]] = {
    "green": (0x2E, 0xE5, 0x9D),
    "amber": (0xF0, 0xA5, 0x00),
    "red": (0xFF, 0x5B, 0x8A),
}


def risk_color(risk: Optional[str]) -> tuple[int, int, int]:
    """Semantic accent RGB for a risk label; purple fallback when unset/unknown.

    Pure + Qt-free so the locked hexes are asserted headlessly. Mirrors
    intent.Intent.risk's vocabulary (launch/reversible/irreversible/ambiguous).
    """
    return _RISK_RGB.get(risk or "", _PURPLE)


def format_latency_chip(latency_ms: float, grade_str: str) -> str:
    """Format the 'DID IT IN N ms' chip text.

    Pure helper — no Qt, no screen, fully headless. grade_str is the output of
    latency.grade() but is accepted for label generation parity.
    """
    return f"DID IT IN {int(round(latency_ms))} ms"


def _accent_hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"#{r:02x}{g:02x}{b:02x}"


# ---- widget (Qt lazy) -------------------------------------------------------
if not HEADLESS:
    from PyQt6.QtCore import (
        Qt, QRectF, QTimer,
        QPropertyAnimation, QSequentialAnimationGroup,
        QParallelAnimationGroup, pyqtProperty,
    )
    from PyQt6.QtGui import (
        QColor, QFont, QPainter, QPainterPath, QPen, QLinearGradient,
    )
    from PyQt6.QtWidgets import (
        QApplication,
        QGraphicsDropShadowEffect,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    _CARD_W = 380
    _MARGIN = 18

    def _rgba(rgb: tuple[int, int, int], a: int = 255) -> str:
        r, g, b = rgb
        return f"rgba({r},{g},{b},{a / 255:.3f})"

    class PreviewCardWidget(QWidget):
        """Frosted Console card: borderless, always-on-top confirm/audit panel.

        show_card(): full card with optional confirm/cancel gating.
        show_status(): transient status mode (UI-06).
        show_done(): latency count-up chip (UI-07).
        show_undo_toast(): countdown undo pill (UI-11).
        dismiss(): animated exit.
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
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

            self._accent: tuple[int, int, int] = _PURPLE
            self._accent_target: tuple[int, int, int] = _PURPLE
            self._on_confirm: Optional[Callable[[], None]] = None
            self._on_cancel: Optional[Callable[[], None]] = None
            self._fired = False

            self._dismiss = QTimer(self)
            self._dismiss.setSingleShot(True)
            self._dismiss.timeout.connect(self._auto_dismiss)

            self._pinned = False

            # UI-06: status mode
            self._status_mode = False
            self._progress_pct: float = 0.0

            # UI-14: animation properties
            self._y_offset_val: int = 0
            self._opacity_val: float = 1.0
            self._brighten_val: float = 0.0
            self._entry_anim: Optional[QSequentialAnimationGroup] = None
            self._exit_anim: Optional[QPropertyAnimation] = None

            # UI-14: risk cross-fade
            self._risk_xfade_t: float = 1.0
            self._risk_xfade_src: tuple[int, int, int] = _PURPLE
            self._risk_xfade_timer = QTimer(self)
            self._risk_xfade_timer.setInterval(16)
            self._risk_xfade_timer.timeout.connect(self._step_risk_xfade)

            # UI-07: latency chip
            self._chip_target_ms: float = 0.0
            self._chip_elapsed_ms: float = 0.0
            self._chip_timer = QTimer(self)
            self._chip_timer.setInterval(16)
            self._chip_timer.timeout.connect(self._step_chip_countup)

            # UI-11: undo toast
            self._toast_countdown: float = 0.0
            self._toast_on_undo: Optional[Callable[[], None]] = None
            self._toast_timer = QTimer(self)
            self._toast_timer.setInterval(100)
            self._toast_timer.timeout.connect(self._step_toast)
            self._toast_text: str = ""

            # UI-12: adaptive ink 2fps sampler
            self._panel_rgb: tuple[int, int, int] = _PANEL_RGB
            self._panel_alpha: int = _PANEL_ALPHA
            self._ink_timer = QTimer(self)
            self._ink_timer.setInterval(500)
            self._ink_timer.timeout.connect(self._sample_background)

            self._build_ui()

        # -- pyqtProperty for animations --------------------------------------- #

        def _get_y_offset(self) -> int:
            return self._y_offset_val

        def _set_y_offset(self, v: int) -> None:
            self._y_offset_val = v
            self.update()

        y_offset = pyqtProperty(int, fget=_get_y_offset, fset=_set_y_offset)

        def _get_opacity(self) -> float:
            return self._opacity_val

        def _set_opacity(self, v: float) -> None:
            self._opacity_val = v
            self.update()

        opacity_val = pyqtProperty(float, fget=_get_opacity, fset=_set_opacity)

        def _get_brighten(self) -> float:
            return self._brighten_val

        def _set_brighten(self, v: float) -> None:
            self._brighten_val = v
            self.update()

        brighten_val = pyqtProperty(float, fget=_get_brighten, fset=_set_brighten)

        # -- construction --------------------------------------------------- #

        def _build_ui(self) -> None:
            outer = QVBoxLayout(self)
            outer.setContentsMargins(_MARGIN, _MARGIN, _MARGIN, _MARGIN)
            outer.setSpacing(8)

            # UI-06: phase label (status mode)
            self._status_label = QLabel("")
            self._status_label.setFont(QFont("SF Mono", 9, QFont.Weight.Bold))
            self._status_label.setStyleSheet(
                "color: #22d3ee; background: transparent; letter-spacing: 2px;"
            )
            self._status_label.hide()

            self._title = QLabel("")
            self._title.setFont(QFont("SF Pro Display", 16, QFont.Weight.DemiBold))
            self._title.setStyleSheet("color: #f3f4fa; background: transparent;")
            self._title.setWordWrap(True)

            self._gloss = QLabel("")
            self._gloss.setFont(QFont("SF Pro Text", 12))
            self._gloss.setStyleSheet("color: #c8ccda; background: transparent;")
            self._gloss.setWordWrap(True)

            self._literal = QLabel("")
            self._literal.setFont(QFont("SF Mono", 10))
            self._literal.setStyleSheet("color: #8b90a3; background: transparent;")
            self._literal.setWordWrap(True)

            self._badge = QLabel("")
            self._badge.setFont(QFont("SF Mono", 9, QFont.Weight.Bold))
            self._badge.setStyleSheet(
                "color: #b08eff; background: transparent; letter-spacing: 1px;"
            )

            # UI-07: latency chip
            self._chip_label = QLabel("")
            self._chip_label.setFont(QFont("SF Mono", 9, QFont.Weight.Bold))
            self._chip_label.setStyleSheet(
                "color: #2ee59d; background: transparent; letter-spacing: 1px;"
            )
            self._chip_label.hide()

            # UI-11: toast countdown label
            self._toast_label = QLabel("")
            self._toast_label.setFont(QFont("SF Pro Text", 10))
            self._toast_label.setStyleSheet("color: #c8ccda; background: transparent;")
            self._toast_label.hide()

            outer.addWidget(self._status_label)
            outer.addWidget(self._title)
            outer.addWidget(self._gloss)
            outer.addWidget(self._literal)
            outer.addWidget(self._badge)
            outer.addWidget(self._chip_label)
            outer.addWidget(self._toast_label)

            # Confirm/Cancel row
            self._btn_row = QWidget()
            row = QHBoxLayout(self._btn_row)
            row.setContentsMargins(0, 6, 0, 0)
            row.setSpacing(10)
            row.addStretch(1)
            self._cancel_btn = QPushButton("Cancel")
            self._confirm_btn = QPushButton("Confirm")
            for b in (self._cancel_btn, self._confirm_btn):
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setFixedHeight(28)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self._cancel_btn.clicked.connect(self._on_cancel_click)
            self._confirm_btn.clicked.connect(self._on_confirm_click)
            row.addWidget(self._cancel_btn)
            row.addWidget(self._confirm_btn)
            outer.addWidget(self._btn_row)
            self._btn_row.hide()

            # UI-11: undo row
            self._undo_row = QWidget()
            undo_layout = QHBoxLayout(self._undo_row)
            undo_layout.setContentsMargins(0, 4, 0, 0)
            undo_layout.setSpacing(8)
            undo_layout.addStretch(1)
            self._undo_btn = QPushButton("Undo")
            self._undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._undo_btn.setFixedHeight(24)
            self._undo_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self._undo_btn.setStyleSheet(
                "QPushButton {"
                "  background: rgba(176,142,255,0.15); color: #b08eff;"
                "  border: 1px solid rgba(176,142,255,0.30); border-radius: 6px;"
                "  padding: 2px 10px; font-size: 10px;"
                "}"
                "QPushButton:hover { background: rgba(176,142,255,0.25); }"
            )
            self._undo_btn.clicked.connect(self._on_undo_click)
            undo_layout.addWidget(self._undo_btn)
            outer.addWidget(self._undo_row)
            self._undo_row.hide()

            self.setFixedWidth(_CARD_W)

            self._glow = QGraphicsDropShadowEffect(self)
            self._glow.setBlurRadius(34)
            self._glow.setOffset(0, 0)
            self._glow.setColor(QColor(*_PURPLE, 150))
            self.setGraphicsEffect(self._glow)

        # -- public API ----------------------------------------------------- #

        def show_card(
            self,
            card: PreviewCard,
            on_confirm: Optional[Callable[[], None]] = None,
            on_cancel: Optional[Callable[[], None]] = None,
            auto_dismiss_ms: Optional[int] = None,
            latency: Optional[dict] = None,
        ) -> None:
            """Render card; gate on Confirm/Cancel iff on_confirm is given.

            latency: optional dict with 'total_ms' key — triggers animated chip.
            """
            self._status_mode = False
            self._status_label.hide()
            self._fired = False
            self._on_confirm = on_confirm
            self._on_cancel = on_cancel

            new_accent = risk_color(getattr(card, "risk", None))
            self._start_risk_xfade(new_accent)

            self._title.setText(card.title or "")
            self._gloss.setText(card.gloss or "")
            self._gloss.setVisible(bool(card.gloss))
            self._literal.setText(card.literal or "")
            self._literal.setVisible(bool(card.literal))
            self._badge.setText(self._badge_text(card))
            self._chip_label.hide()
            self._chip_timer.stop()
            self._toast_label.hide()
            self._undo_row.hide()
            self._toast_timer.stop()

            gated = on_confirm is not None
            self._btn_row.setVisible(gated)
            if gated:
                self._style_buttons(self._accent)

            self._glow.setColor(QColor(*self._accent, 160))

            self.adjustSize()
            self._place_top_right()
            self._play_entry_animation()
            self.show()
            self.raise_()
            self._apply_macwin()
            self._ink_timer.start()

            self._dismiss.stop()
            if not gated:
                ms = DEFAULT_AUTO_DISMISS_MS if auto_dismiss_ms is None else auto_dismiss_ms
                if ms and ms > 0:
                    self._dismiss.start(int(ms))
            self.update()

            if latency:
                total_ms = float(latency.get("total_ms", 0.0))
                if total_ms > 0:
                    self._start_chip_countup(total_ms)

        def dismiss(self) -> None:
            """Animated exit; stop all timers."""
            self._dismiss.stop()
            self._chip_timer.stop()
            self._toast_timer.stop()
            self._ink_timer.stop()
            self._play_exit_animation()

        def show_status(self, phase_str: str, text: str) -> None:
            """UI-06: transient status overlay.

            Shows phase label + partial content + animated top-edge progress bar.
            Call repeatedly to stream partial text; show_card() replaces it.
            """
            self._status_mode = True
            self._chip_label.hide()
            self._chip_timer.stop()
            self._toast_label.hide()
            self._undo_row.hide()
            self._toast_timer.stop()
            self._btn_row.hide()
            self._fired = False

            try:
                from ..overlay import phase as ph
                new_accent = ph.accent(phase_str)
                self._start_risk_xfade(new_accent)
                phase_label = phase_str.upper()
            except Exception:
                phase_label = (phase_str or "").upper()

            self._status_label.setText(phase_label)
            self._status_label.setStyleSheet(
                f"color: {_accent_hex(self._accent)}; background: transparent; "
                "letter-spacing: 2px;"
            )
            self._status_label.show()
            self._title.setText(text or "")
            self._gloss.hide()
            self._literal.hide()
            self._badge.setText("")
            self._progress_pct = 0.4  # partial bar while in status mode

            if not self.isVisible():
                self.adjustSize()
                self._place_top_right()
                self._play_entry_animation()
                self.show()
                self.raise_()
                self._apply_macwin()
                self._ink_timer.start()
            else:
                self.adjustSize()
                self.update()

        def show_done(self, latency: dict) -> None:
            """UI-07: animated 'DID IT IN N ms' count-up chip.

            latency: dict with 'total_ms'. Color follows latency.grade().
            """
            total_ms = float(latency.get("total_ms", 0.0))
            if total_ms > 0:
                self._start_chip_countup(total_ms)

        def show_undo_toast(
            self,
            label: str,
            seconds: float,
            on_undo: Callable[[], None],
        ) -> None:
            """UI-11: countdown undo pill.

            label: action description. seconds: countdown. on_undo: click handler.
            Appends below the card content; does NOT replace the card.
            """
            self._toast_text = label
            self._toast_countdown = float(seconds)
            self._toast_on_undo = on_undo
            self._update_toast_display()
            self._undo_row.show()
            self._toast_timer.start()
            self.adjustSize()
            self.update()

        # -- UI-07 chip internals ------------------------------------------- #

        def _start_chip_countup(self, target_ms: float) -> None:
            try:
                from ..latency import grade
                g = grade(target_ms)
            except Exception:
                g = "green"

            self._chip_target_ms = target_ms
            self._chip_elapsed_ms = 0.0
            chip_rgb = _CHIP_COLORS.get(g, _CHIP_COLORS["green"])
            r, gv, b = chip_rgb
            self._chip_label.setStyleSheet(
                f"color: rgb({r},{gv},{b}); background: transparent; "
                "letter-spacing: 1px;"
            )
            self._chip_label.setText("DID IT IN 0 ms")
            self._chip_label.show()
            self._chip_timer.start()

        def _step_chip_countup(self) -> None:
            chip_duration = 260.0  # ms — matches motion.DURATIONS['chip_count']
            self._chip_elapsed_ms += 16.0
            t = min(1.0, self._chip_elapsed_ms / chip_duration)

            try:
                from ..overlay.motion import ease_out_cubic
                eased = ease_out_cubic(t)
            except Exception:
                eased = t

            current = int(round(eased * self._chip_target_ms))
            self._chip_label.setText(f"DID IT IN {current} ms")

            if t >= 1.0:
                self._chip_timer.stop()
                self._chip_label.setText(format_latency_chip(self._chip_target_ms, ""))

        # -- UI-11 toast internals ------------------------------------------ #

        def _update_toast_display(self) -> None:
            secs = max(0.0, self._toast_countdown)
            self._toast_label.setText(f"{self._toast_text} ({secs:.1f}s)")
            self._toast_label.show()

        def _step_toast(self) -> None:
            self._toast_countdown -= 0.1
            if self._toast_countdown <= 0.0:
                self._toast_timer.stop()
                self._toast_label.hide()
                self._undo_row.hide()
                self.adjustSize()
                self.update()
            else:
                self._update_toast_display()

        def _on_undo_click(self) -> None:
            self._toast_timer.stop()
            self._toast_label.hide()
            self._undo_row.hide()
            fn = self._toast_on_undo
            if fn is not None:
                try:
                    fn()
                except Exception:
                    pass
            self.adjustSize()
            self.update()

        # -- UI-14 animation ------------------------------------------------- #

        def _play_entry_animation(self) -> None:
            """80ms opacity OutCubic + 120ms slide-in + 60ms accent brighten."""
            try:
                if self._entry_anim is not None:
                    self._entry_anim.stop()
                    self._entry_anim = None

                from ..overlay.motion import curve

                self._y_offset_val = 14
                self._opacity_val = 0.0
                self._brighten_val = 0.0

                seq = QSequentialAnimationGroup(self)

                # Parallel: opacity fade-in + slide-in
                parallel = QParallelAnimationGroup(seq)

                fade = QPropertyAnimation(self, b"opacity_val", parallel)
                fade.setDuration(80)
                fade.setStartValue(0.0)
                fade.setEndValue(1.0)
                fade.setEasingCurve(curve("in"))

                slide = QPropertyAnimation(self, b"y_offset", parallel)
                slide.setDuration(120)
                slide.setStartValue(14)
                slide.setEndValue(0)
                slide.setEasingCurve(curve("in"))

                seq.addAnimation(parallel)

                # Brief accent brighten flash
                brighten = QPropertyAnimation(self, b"brighten_val", seq)
                brighten.setDuration(60)
                brighten.setStartValue(0.3)
                brighten.setEndValue(0.0)
                brighten.setEasingCurve(curve("out"))

                seq.addAnimation(brighten)

                self._entry_anim = seq
                seq.start()
            except Exception:
                # Fallback: show immediately
                self._y_offset_val = 0
                self._opacity_val = 1.0
                self._brighten_val = 0.0
                self.update()

        def _play_exit_animation(self) -> None:
            """Fade out over 120ms then hide."""
            try:
                from ..overlay.motion import curve
                anim = QPropertyAnimation(self, b"opacity_val", self)
                anim.setDuration(120)
                anim.setStartValue(self._opacity_val)
                anim.setEndValue(0.0)
                anim.setEasingCurve(curve("out"))
                anim.finished.connect(self._finish_exit)
                self._exit_anim = anim
                anim.start()
            except Exception:
                self.hide()

        def _finish_exit(self) -> None:
            self._opacity_val = 1.0
            self.hide()

        # -- UI-14 risk cross-fade ------------------------------------------ #

        def _start_risk_xfade(self, new_accent: tuple[int, int, int]) -> None:
            """Begin a 40ms cross-fade from current _accent to new_accent."""
            if new_accent == self._accent:
                return
            self._risk_xfade_src = self._accent
            self._accent_target = new_accent
            self._risk_xfade_t = 0.0
            self._risk_xfade_timer.start()

        def _step_risk_xfade(self) -> None:
            """Advance risk color cross-fade (~40ms total at ~60fps)."""
            xfade_duration = 40.0
            step = 16.0 / xfade_duration
            self._risk_xfade_t = min(1.0, self._risk_xfade_t + step)

            try:
                from ..overlay.motion import lerp_rgb
                blended = lerp_rgb(
                    self._risk_xfade_src, self._accent_target, self._risk_xfade_t
                )
            except Exception:
                blended = self._accent_target

            self._accent = tuple(int(c) for c in blended)  # type: ignore[assignment]
            self._glow.setColor(QColor(*self._accent, 160))
            self.update()

            if self._risk_xfade_t >= 1.0:
                self._risk_xfade_timer.stop()
                self._accent = self._accent_target

        # -- UI-12 adaptive ink ----------------------------------------------- #

        def _sample_background(self) -> None:
            """2fps: sample pixels behind the card; update panel style via AdaptiveInk."""
            try:
                from ..overlay.adaptive_ink import AdaptiveInk
                from ..screen import grab_region

                geo = self.geometry()
                # grab_region(x, y, radius) — use card center + half-diagonal as radius
                cx = geo.x() + geo.width() // 2
                cy = geo.y() + geo.height() // 2
                radius = max(geo.width(), geo.height()) // 2 + 10
                img = grab_region(cx, cy, radius)
                if img is not None:
                    ink = AdaptiveInk()
                    rgb, alpha = ink.panel_style(background=img)
                    self._panel_rgb = rgb
                    self._panel_alpha = alpha
                    self.update()
            except Exception:
                pass  # best-effort; locked defaults remain

        # -- helpers --------------------------------------------------------- #

        @staticmethod
        def _badge_text(card: PreviewCard) -> str:
            mech = (card.mechanism or "").strip()
            lat = getattr(card, "latency_ms", None)
            if mech and lat:
                return f"{mech.upper()}  ·  {float(lat):.0f} ms"
            if mech:
                return mech.upper()
            if lat:
                return f"{float(lat):.0f} ms"
            return ""

        def _style_buttons(self, accent: tuple[int, int, int]) -> None:
            self._confirm_btn.setStyleSheet(
                "QPushButton {"
                f"  background: {_rgba(accent, 235)};"
                "  color: #10121a; border: none; border-radius: 8px;"
                "  padding: 4px 16px; font-weight: 600;"
                "}"
                "QPushButton:hover { background: " + _rgba(accent, 255) + "; }"
            )
            self._cancel_btn.setStyleSheet(
                "QPushButton {"
                "  background: rgba(255,255,255,0.06); color: #c8ccda;"
                "  border: 1px solid rgba(255,255,255,0.10); border-radius: 8px;"
                "  padding: 4px 16px;"
                "}"
                "QPushButton:hover { background: rgba(255,255,255,0.12); }"
            )

        def _place_top_right(self) -> None:
            scr = QApplication.primaryScreen()
            if scr is None:
                return
            geo = scr.availableGeometry()
            x = geo.right() - self.width() - 24
            y = geo.top() + 24
            self.move(int(x), int(y))

        def _apply_macwin(self) -> None:
            if self._pinned:
                return
            from ..macwin import make_always_visible
            make_always_visible(self, click_through=False)
            self._pinned = True

        # -- callbacks ------------------------------------------------------ #

        def _on_confirm_click(self) -> None:
            if self._fired:
                return
            self._fired = True
            self._dismiss.stop()
            cb = self._on_confirm
            self.hide()
            if cb is not None:
                cb()

        def _on_cancel_click(self) -> None:
            if self._fired:
                return
            self._fired = True
            self._dismiss.stop()
            cb = self._on_cancel
            self.hide()
            if cb is not None:
                cb()

        def _auto_dismiss(self) -> None:
            if self._fired:
                return
            self._fired = True
            cb = self._on_cancel
            self._ink_timer.stop()
            self.hide()
            if cb is not None:
                cb()

        # -- painting ------------------------------------------------------- #

        def paintEvent(self, _event) -> None:  # noqa: N802
            """Draw frosted panel, border, risk edge, UI-06 progress bar.

            Applies UI-14 opacity/slide transforms. Uses adaptive panel color
            from UI-12 sampler (_panel_rgb/_panel_alpha).
            """
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

            # UI-14: opacity fade
            p.setOpacity(max(0.0, min(1.0, self._opacity_val)))

            # UI-14: y-offset slide-in
            if self._y_offset_val != 0:
                p.translate(0, self._y_offset_val)

            inset = 2.0
            rect = QRectF(
                inset, inset, self.width() - 2 * inset, self.height() - 2 * inset
            )
            path = QPainterPath()
            path.addRoundedRect(rect, _CARD_RADIUS, _CARD_RADIUS)

            # UI-12: adaptive panel color (updated 2fps by _sample_background)
            panel_rgb = self._panel_rgb
            panel_alpha = self._panel_alpha

            # UI-14: accent brighten on entry flash
            accent = self._accent
            if self._brighten_val > 0.0:
                try:
                    from ..overlay.motion import lerp_rgb
                    accent = lerp_rgb(accent, (255, 255, 255), self._brighten_val * 0.3)
                except Exception:
                    pass

            # Frosted fill
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(*panel_rgb, panel_alpha))
            p.drawPath(path)

            # 1px structural border
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(*_BORDER_RGB, 255), 1.0))
            p.drawPath(path)

            # Semantic risk edge — 2px accent stroke
            r, g, b = accent
            inner = rect.adjusted(1.5, 1.5, -1.5, -1.5)
            ip = QPainterPath()
            ip.addRoundedRect(inner, _CARD_RADIUS - 1.5, _CARD_RADIUS - 1.5)
            p.setPen(QPen(QColor(r, g, b, 220), 2.0))
            p.drawPath(ip)

            # UI-06: animated top-edge progress bar in status mode
            if self._status_mode and self._progress_pct > 0:
                bar_w = max(1, int(self.width() * self._progress_pct))
                bar_h = 3
                bar_x = int(inset)
                bar_y = int(inset)
                grad = QLinearGradient(bar_x, bar_y, bar_x + bar_w, bar_y)
                ar, ag, ab = self._accent
                grad.setColorAt(0.0, QColor(ar, ag, ab, 0))
                grad.setColorAt(0.5, QColor(ar, ag, ab, 220))
                grad.setColorAt(1.0, QColor(ar, ag, ab, 60))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(grad)
                progress_path = QPainterPath()
                progress_path.addRoundedRect(
                    QRectF(bar_x, bar_y, bar_w, bar_h), 1.5, 1.5
                )
                p.drawPath(progress_path)

else:  # HEADLESS stub

    class PreviewCardWidget:  # type: ignore[no-redef]
        """Stub on headless boxes; the real card needs PyQt6 + a display."""

        def __init__(self, *_, **__):
            raise RuntimeError(
                "PreviewCardWidget requires PyQt6 + a display (HEADLESS import). "
                "The pure helper risk_color() is available without Qt."
            )


__all__ = [
    "PreviewCardWidget",
    "risk_color",
    "format_latency_chip",
    "HEADLESS",
    "DEFAULT_AUTO_DISMISS_MS",
]

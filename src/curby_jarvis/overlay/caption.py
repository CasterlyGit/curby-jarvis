"""Transient speech-caption overlay (UI-03) — running partial transcript.

WHY: While the user is mid-utterance the STT engine emits partial text. Showing
it directly below the reticle (or a fixed screen position) lets the user verify
the system heard them correctly and correct before command dispatch. The widget
follows the same self-contrasting (white over 1px black keyline) approach used by
reticle.py so it stays legible over any background.

HEADLESS CONTRACT: importing this module must NOT touch PyQt6, AppKit, or any
display. All Qt symbols are imported lazily inside methods. HEADLESS is True when
PyQt6 is absent. The pure helper ``fit_caption`` is always importable and testable
without any Qt / display.

Layout: a pill-shaped frosted-glass badge (dark matte ground, 40% blur, subtle
cyan keyline on LISTENING/HEARD). The widget is positioned ~40 logical pixels
below a supplied (x, y) anchor — typically the reticle centre — via ``set_pos``.
"""
from __future__ import annotations

from typing import Optional

# ---- headless probe ---------------------------------------------------------
try:  # pragma: no cover
    import PyQt6  # noqa: F401

    HEADLESS = False
except Exception:
    HEADLESS = True

# ---- aesthetic constants ----------------------------------------------------
# Pill geometry
_PILL_H = 36          # height of the caption pill (logical px)
_PILL_PAD_X = 14      # horizontal text padding inside pill
_PILL_RADIUS = 10.0   # rounded rect corner radius
_BELOW_ANCHOR = 40    # gap from anchor point to pill top

# Typography
_FONT_SIZE = 14
_MAX_CHARS_DEFAULT = 60

# Background (dark frosted)
_BG_RGB = (0x14, 0x16, 0x20)   # near-black with a faint blue tint
_BG_ALPHA = 210
_BORDER_RGB = (0x22, 0xD3, 0xEE)  # cyan neon
_BORDER_ALPHA = 160


# ---- pure helper (headless-safe) -------------------------------------------

def fit_caption(text: str, max_chars: int = _MAX_CHARS_DEFAULT) -> str:
    """Trim a potentially long partial transcript to ``max_chars``.

    Strategy: keep the TAIL of the string (most recent words) rather than the
    head so the user always sees what was just spoken. A leading ellipsis is
    prepended when truncation occurs.

    Pure, no Qt, unit-testable headlessly.

    Examples::

        fit_caption("hello world", 20) == "hello world"
        fit_caption("a" * 70, 60) starts with "…"
    """
    if max_chars <= 0:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    # Keep the rightmost max_chars-1 chars and prefix with the ellipsis char.
    return "…" + text[-(max_chars - 1):]


# ---- widget (Qt lazy) -------------------------------------------------------

if not HEADLESS:
    from PyQt6.QtWidgets import QWidget, QApplication
    from PyQt6.QtCore import Qt, QRectF, QTimer
    from PyQt6.QtGui import (
        QPainter, QColor, QPen, QBrush, QPainterPath, QFont, QFontMetrics,
    )

    class CaptionWidget(QWidget):
        """Frameless, click-through caption pill for partial STT transcripts.

        Usage::

            cap = CaptionWidget()
            cap.set_pos(640, 400)          # anchor point (reticle centre)
            cap.show_text("open Spotify")  # called on each partial
            cap.fade_out()                 # called on utterance end

        The integrator wires ``_Bridge.partial`` → ``show_text`` and
        ``_Bridge.phase`` → ``fade_out`` when phase reaches HEARD/DONE/ERROR.
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

            self._text: str = ""
            self._anchor_x: float = 200.0
            self._anchor_y: float = 200.0
            self._fade_alpha: int = 255    # 0..255; counts down on fade
            self._pinned: bool = False

            # Fade-out: reduce alpha 10 steps over ~200ms (every 20ms)
            self._fade_timer = QTimer(self)
            self._fade_timer.timeout.connect(self._tick_fade)

        # -- public API ------------------------------------------------------ #

        def show_text(self, text: str) -> None:
            """Render *text* as the running partial caption."""
            self._fade_timer.stop()
            self._fade_alpha = 255
            self._text = fit_caption(text)
            self._reposition()
            if not self.isVisible():
                self.show()
            self._pin_once()
            self.update()

        def set_pos(self, x: float, y: float) -> None:
            """Set the anchor point (reticle centre); pill appears below it."""
            self._anchor_x = float(x)
            self._anchor_y = float(y)
            if self.isVisible():
                self._reposition()

        def fade_out(self) -> None:
            """Begin a short fade-out; widget hides itself when alpha reaches 0."""
            if not self.isVisible():
                return
            self._fade_timer.start(20)  # ~200ms / 10 steps

        # -- internals ------------------------------------------------------- #

        def _reposition(self) -> None:
            fm = QFontMetrics(self._font())
            text_w = fm.horizontalAdvance(self._text or " ")
            w = text_w + 2 * _PILL_PAD_X
            h = _PILL_H
            x = int(self._anchor_x - w / 2)
            y = int(self._anchor_y + _BELOW_ANCHOR)
            self.setGeometry(x, y, w, h)

        def _pin_once(self) -> None:
            if not self._pinned:
                try:
                    from ..macwin import make_always_visible
                    make_always_visible(self, click_through=True)
                except Exception:
                    pass
                self._pinned = True

        def _font(self) -> "QFont":
            f = QFont()
            f.setPointSize(_FONT_SIZE)
            f.setWeight(QFont.Weight.DemiBold)
            return f

        def _tick_fade(self) -> None:
            self._fade_alpha = max(0, self._fade_alpha - 26)  # 10 steps to 0
            self.update()
            if self._fade_alpha == 0:
                self._fade_timer.stop()
                self.hide()

        def paintEvent(self, _event) -> None:  # noqa: N802
            if not self._text:
                return
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            a = self._fade_alpha

            # -- pill background (frosted dark)
            r, g, b = _BG_RGB
            bg = QColor(r, g, b, int(_BG_ALPHA * a / 255))
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, self.width(), self.height()), _PILL_RADIUS, _PILL_RADIUS)
            p.fillPath(path, QBrush(bg))

            # -- cyan keyline border
            br, bg2, bb = _BORDER_RGB
            pen = QPen(QColor(br, bg2, bb, int(_BORDER_ALPHA * a / 255)), 1.0)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), _PILL_RADIUS, _PILL_RADIUS)

            # -- text: black keyline under white cap (self-contrast trick)
            p.setFont(self._font())
            text_rect = QRectF(_PILL_PAD_X, 0, self.width() - 2 * _PILL_PAD_X, self.height())
            # shadow / keyline
            p.setPen(QColor(0, 0, 0, int(200 * a / 255)))
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                p.drawText(text_rect.translated(dx, dy), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._text)
            # white cap
            p.setPen(QColor(255, 255, 255, a))
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._text)

else:

    class CaptionWidget:  # type: ignore[no-redef]
        """Stub for headless environments; raises on construction."""

        def __init__(self, *_, **__):
            raise RuntimeError(
                "CaptionWidget requires PyQt6 + a display. "
                "The pure helper fit_caption is available without Qt."
            )


__all__ = ["HEADLESS", "fit_caption", "CaptionWidget"]

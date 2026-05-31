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

Threading: the widget never spawns threads. Per the overlay contract, the app owns
the single _Bridge and marshals confirm/cancel onto the Qt main thread; the
callbacks passed here are already main-thread-safe by the time show_card runs.
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
# Touch only the import system, never a QApplication, so a headless CI box with
# no display can import this module to reach `risk_color` without spinning up Qt.
try:  # pragma: no cover - trivial availability flag
    import PyQt6  # noqa: F401

    HEADLESS = False
except Exception:  # PyQt6 absent -> module still imports; widget unusable
    HEADLESS = True


# ---- locked Frosted Console palette -----------------------------------------
# Semantic risk -> accent RGB. These are the LOCKED hexes from the overlay spec;
# they intentionally match overlay/reticle.RISK_COLORS so card and reticle agree.
_RISK_RGB: dict[str, tuple[int, int, int]] = {
    RISK_LAUNCH: (0x00, 0xE4, 0xFF),        # cyan  — app open/run
    RISK_REVERSIBLE: (0x2E, 0xE5, 0x9D),    # mint  — auto-runnable
    RISK_IRREVERSIBLE: (0xFF, 0x5B, 0x8A),  # rose  — destructive, always confirm
    RISK_AMBIGUOUS: (0xF0, 0xA5, 0x00),     # amber — unresolved deixis / low conf
}
# Soft purple — the neutral outer glow AND the fallback accent for an unset risk.
_PURPLE = (0xB0, 0x8E, 0xFF)

# Frosted panel + border (locked).
_PANEL_RGB = (0x1A, 0x1D, 0x27)
_PANEL_ALPHA = 244
_BORDER_RGB = (0x23, 0x26, 0x31)
_CARD_RADIUS = 12

# Default time an ungated (no-confirm) card stays up before auto-dismiss.
DEFAULT_AUTO_DISMISS_MS = 2600


def risk_color(risk: Optional[str]) -> tuple[int, int, int]:
    """Semantic accent RGB for a risk label; purple fallback when unset/unknown.

    Pure + Qt-free so the locked hexes are asserted headlessly. Mirrors
    intent.Intent.risk's vocabulary (launch/reversible/irreversible/ambiguous).
    """
    return _RISK_RGB.get(risk or "", _PURPLE)


# ---- widget (Qt lazy) -------------------------------------------------------
# Defined only when PyQt6 imported, so a headless `import` never references a Qt
# base class. Headless callers that try to construct it get a clear error.

if not HEADLESS:
    from PyQt6.QtCore import Qt, QRectF, QTimer
    from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
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
    _MARGIN = 18  # inner content margin; also the glow pad outside the panel

    def _rgba(rgb: tuple[int, int, int], a: int = 255) -> str:
        r, g, b = rgb
        return f"rgba({r},{g},{b},{a / 255:.3f})"

    class PreviewCardWidget(QWidget):
        """Frosted Console card: a borderless, always-on-top confirm/audit panel.

        Call `show_card(card, on_confirm, on_cancel, auto_dismiss_ms)` to render.
        With an `on_confirm` callback the card is GATED: it shows Confirm/Cancel
        and waits. With no `on_confirm` it is informational and auto-dismisses
        after `auto_dismiss_ms` (DEFAULT_AUTO_DISMISS_MS if None). The risk color
        drives the panel's edge + outer glow.
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
            self._on_confirm: Optional[Callable[[], None]] = None
            self._on_cancel: Optional[Callable[[], None]] = None
            self._fired = False  # guard: a card fires confirm/cancel at most once

            self._dismiss = QTimer(self)
            self._dismiss.setSingleShot(True)
            self._dismiss.timeout.connect(self._auto_dismiss)

            self._pinned = False  # macwin treatment applied once, post first show
            self._build_ui()

        # -- construction --------------------------------------------------- #

        def _build_ui(self) -> None:
            """Lay out the static label/button skeleton once; text is set per-card.

            The painted frost panel is drawn in paintEvent; this layout only
            positions text + buttons inside an inset that leaves room for the glow.
            """
            outer = QVBoxLayout(self)
            outer.setContentsMargins(_MARGIN, _MARGIN, _MARGIN, _MARGIN)
            outer.setSpacing(8)

            self._title = QLabel("")
            self._title.setFont(QFont("SF Pro Display", 16, QFont.Weight.DemiBold))
            self._title.setStyleSheet("color: #f3f4fa; background: transparent;")
            self._title.setWordWrap(True)

            self._gloss = QLabel("")
            self._gloss.setFont(QFont("SF Pro Text", 12))
            self._gloss.setStyleSheet("color: #c8ccda; background: transparent;")
            self._gloss.setWordWrap(True)

            self._literal = QLabel("")
            # Monospace for the audit line: URL / AppleScript / coordinate read.
            self._literal.setFont(QFont("SF Mono", 10))
            self._literal.setStyleSheet("color: #8b90a3; background: transparent;")
            self._literal.setWordWrap(True)

            self._badge = QLabel("")
            self._badge.setFont(QFont("SF Mono", 9, QFont.Weight.Bold))
            self._badge.setStyleSheet(
                "color: #b08eff; background: transparent; letter-spacing: 1px;"
            )

            outer.addWidget(self._title)
            outer.addWidget(self._gloss)
            outer.addWidget(self._literal)
            outer.addWidget(self._badge)

            # Confirm/Cancel row — hidden until a gated card needs it.
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

            self.setFixedWidth(_CARD_W)

            # Soft purple outer glow via a drop shadow; recolored per risk on show.
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
        ) -> None:
            """Render `card`; gate on Confirm/Cancel iff `on_confirm` is given.

            `on_confirm`/`on_cancel` must already be safe to call on the Qt main
            thread (the app marshals them through its _Bridge). With no
            `on_confirm` the card is informational and auto-dismisses after
            `auto_dismiss_ms` (DEFAULT_AUTO_DISMISS_MS when None).
            """
            self._fired = False
            self._on_confirm = on_confirm
            self._on_cancel = on_cancel
            self._accent = risk_color(getattr(card, "risk", None))

            self._title.setText(card.title or "")
            self._gloss.setText(card.gloss or "")
            self._gloss.setVisible(bool(card.gloss))
            self._literal.setText(card.literal or "")
            self._literal.setVisible(bool(card.literal))
            self._badge.setText(self._badge_text(card))

            gated = on_confirm is not None
            self._btn_row.setVisible(gated)
            if gated:
                self._style_buttons(self._accent)

            # Recolor the outer glow toward the risk accent (kept soft).
            self._glow.setColor(QColor(*self._accent, 160))

            self.adjustSize()
            self._place_top_right()
            self.show()
            self.raise_()
            self._apply_macwin()

            self._dismiss.stop()
            if not gated:
                ms = DEFAULT_AUTO_DISMISS_MS if auto_dismiss_ms is None else auto_dismiss_ms
                if ms and ms > 0:
                    self._dismiss.start(int(ms))
            self.update()

        def dismiss(self) -> None:
            """Hide the card and stop the auto-dismiss timer (no callback fired)."""
            self._dismiss.stop()
            self.hide()

        # -- helpers (pure-ish, Qt types) ---------------------------------- #

        @staticmethod
        def _badge_text(card: PreviewCard) -> str:
            """Compose the mechanism+latency badge from the card's audit fields.

            latency lives on ConnectorResult, not PreviewCard, so we read an
            optional `latency_ms` attribute if a caller stamped one on; otherwise
            the badge is mechanism-only. Keeps the badge honest about timing.
            """
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
            """Confirm gets the risk accent fill; Cancel stays a quiet ghost."""
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
            """Pin to the primary screen's top-right inset (curby's note corner)."""
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
            # The card is interactive (buttons), so NOT click-through — only the
            # always-on-top / all-spaces treatment, never ignoresMouseEvents.
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
            # Timeout on an ungated card == implicit cancel (fire cancel if given).
            if self._fired:
                return
            self._fired = True
            cb = self._on_cancel
            self.hide()
            if cb is not None:
                cb()

        # -- painting ------------------------------------------------------- #

        def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override name
            """Draw the frosted panel, 1px border, and the semantic risk edge.

            The glow is the QGraphicsDropShadowEffect; here we paint the opaque
            frost (so text contrast is background-independent), the quiet border,
            and a 2px accent stroke just inside the border for the risk color.
            """
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

            # Inset so the drop-shadow glow has room and the stroke isn't clipped.
            inset = 2.0
            rect = QRectF(
                inset, inset, self.width() - 2 * inset, self.height() - 2 * inset
            )
            path = QPainterPath()
            path.addRoundedRect(rect, _CARD_RADIUS, _CARD_RADIUS)

            # Frosted fill — opaque enough that card text never reads the desktop.
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(*_PANEL_RGB, _PANEL_ALPHA))
            p.drawPath(path)

            # Quiet 1px structural border.
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(*_BORDER_RGB, 255), 1.0))
            p.drawPath(path)

            # Semantic risk edge — a 2px accent stroke just inside the border.
            r, g, b = self._accent
            inner = rect.adjusted(1.5, 1.5, -1.5, -1.5)
            ip = QPainterPath()
            ip.addRoundedRect(inner, _CARD_RADIUS - 1.5, _CARD_RADIUS - 1.5)
            p.setPen(QPen(QColor(r, g, b, 220), 2.0))
            p.drawPath(ip)

else:  # HEADLESS: stub so callers get a clear, lazy failure (not an ImportError).

    class PreviewCardWidget:  # type: ignore[no-redef]
        """Stub on headless boxes; the real card needs PyQt6 + a display.

        The pure helper `risk_color` is importable and usable without Qt.
        """

        def __init__(self, *_, **__):
            raise RuntimeError(
                "PreviewCardWidget requires PyQt6 + a display (HEADLESS import). "
                "The pure helper risk_color() is available without Qt."
            )


__all__ = ["PreviewCardWidget", "risk_color", "HEADLESS", "DEFAULT_AUTO_DISMISS_MS"]

"""Frosted history panel overlay (UI-11) — action log + undo chips.

WHY: After a chain of voice commands the user needs to audit what happened,
see latencies, and undo reversible actions without opening a separate app.
This widget reads the telemetry JSONL on demand (lazy: only at ``show_log``
time) so it adds zero runtime cost when hidden. Each row shows verb+target,
mechanism badge, latency, risk dot, and ok/fail; rows with an undo_id show an
'Undo' chip that fires the injected ``on_undo`` callback.

HEADLESS CONTRACT: importing this module must NOT touch PyQt6 or any display.
All Qt symbols are imported lazily. HEADLESS mirrors reticle.py. The pure
helper ``format_row`` is always importable and testable without Qt / display.

Layout: frosted scrollable panel, fixed width, right-anchored (just inside the
right edge of the primary screen), vertically centred. Toggle with ``toggle()``.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

# ---- headless probe ---------------------------------------------------------
try:  # pragma: no cover
    import PyQt6  # noqa: F401

    HEADLESS = False
except Exception:
    HEADLESS = True

# ---- aesthetic constants ----------------------------------------------------
_PANEL_W = 380       # panel width (logical px)
_PANEL_H = 480       # panel max height
_PANEL_PAD = 12
_ROW_H = 48          # row height
_BADGE_FONT_SIZE = 10
_MAIN_FONT_SIZE = 13

# Risk dot colours (RGB) — aligned with reticle.RISK_COLORS
_RISK_DOT: dict[str, tuple[int, int, int]] = {
    "launch":       (0x00, 0xE4, 0xFF),   # cyan
    "reversible":   (0x2E, 0xE5, 0x9D),   # mint
    "irreversible": (0xFF, 0x5B, 0x8A),   # rose
    "ambiguous":    (0xF0, 0xA5, 0x00),   # amber
}
_RISK_DOT_DEFAULT = (0xB0, 0x8E, 0xFF)   # soft purple fallback

_BG_RGB = (0x14, 0x16, 0x20)
_BG_ALPHA = 230
_BORDER_RGB = (0x22, 0xD3, 0xEE)
_BORDER_ALPHA = 80

# Latency thresholds for colour coding (ms)
_LAT_GREEN = 800
_LAT_AMBER = 2000


# ---- pure helper (headless-safe) -------------------------------------------

def format_row(event_dict: dict) -> dict:
    """Derive display fields from a raw telemetry event dict.

    Returns a dict with keys suitable for rendering one history row:

    ``label``       — "<verb> <target>" or best-effort fallback text
    ``mechanism``   — connector/mechanism name string
    ``latency_ms``  — float or None if not present
    ``latency_str`` — human string e.g. "820ms" or "–"
    ``risk``        — risk string or ""
    ``risk_color``  — (r, g, b) tuple
    ``ok``          — bool or None
    ``ok_str``      — "✓" / "✗" / "–"
    ``undo_id``     — undo_id string or None
    ``latency_cls`` — "green" / "amber" / "red" based on latency

    Pure, no Qt, no filesystem, unit-testable headlessly.
    """
    verb = event_dict.get("verb", "")
    target = event_dict.get("target", "")
    label = f"{verb} {target}".strip() or event_dict.get("text", "") or "(event)"

    mechanism = event_dict.get("mechanism", event_dict.get("surface", ""))

    # Latency: prefer explicit total_ms, fall back to latency_ms, then None
    lat = event_dict.get("total_ms") or event_dict.get("latency_ms")
    try:
        latency_ms: Optional[float] = float(lat) if lat is not None else None
    except (TypeError, ValueError):
        latency_ms = None

    if latency_ms is not None:
        latency_str = f"{int(latency_ms)}ms"
        if latency_ms < _LAT_GREEN:
            latency_cls = "green"
        elif latency_ms < _LAT_AMBER:
            latency_cls = "amber"
        else:
            latency_cls = "red"
    else:
        latency_str = "–"
        latency_cls = "green"

    risk = str(event_dict.get("risk", ""))
    risk_color = _RISK_DOT.get(risk, _RISK_DOT_DEFAULT)

    ok_raw = event_dict.get("ok")
    if ok_raw is None:
        ok: Optional[bool] = None
        ok_str = "–"
    else:
        ok = bool(ok_raw)
        ok_str = "✓" if ok else "✗"

    undo_id = event_dict.get("undo_id") or None

    return {
        "label": label,
        "mechanism": mechanism,
        "latency_ms": latency_ms,
        "latency_str": latency_str,
        "latency_cls": latency_cls,
        "risk": risk,
        "risk_color": risk_color,
        "ok": ok,
        "ok_str": ok_str,
        "undo_id": undo_id,
    }


# ---- widget (Qt lazy) -------------------------------------------------------

if not HEADLESS:
    from PyQt6.QtWidgets import (
        QWidget, QApplication, QScrollArea, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QFrame,
    )
    from PyQt6.QtCore import Qt, QRectF, QTimer
    from PyQt6.QtGui import (
        QPainter, QColor, QPainterPath, QBrush, QPen, QFont,
    )

    class _RowWidget(QWidget):
        """One telemetry-event row with label, mechanism badge, latency, risk dot,
        ok/fail indicator, and an optional Undo chip."""

        def __init__(self, row: dict, on_undo: Optional[Callable[[str], None]]) -> None:
            super().__init__()
            self.setFixedHeight(_ROW_H)
            layout = QHBoxLayout(self)
            layout.setContentsMargins(8, 4, 8, 4)
            layout.setSpacing(6)

            # ok indicator
            ok_lbl = QLabel(row["ok_str"])
            ok_lbl.setFixedWidth(16)
            ok_color = "#34D399" if row["ok"] else "#F46378" if row["ok"] is False else "#606060"
            ok_lbl.setStyleSheet(f"color: {ok_color}; font-size: 14px;")
            layout.addWidget(ok_lbl)

            # risk dot (coloured circle label)
            r, g, b = row["risk_color"]
            dot = QLabel("●")
            dot.setStyleSheet(f"color: rgb({r},{g},{b}); font-size: 10px;")
            dot.setFixedWidth(14)
            layout.addWidget(dot)

            # main label
            lbl = QLabel(row["label"])
            f = QFont()
            f.setPointSize(_MAIN_FONT_SIZE)
            lbl.setFont(f)
            lbl.setStyleSheet("color: #E2E8F0;")
            lbl.setWordWrap(False)
            layout.addWidget(lbl, stretch=1)

            # mechanism badge
            if row["mechanism"]:
                badge = QLabel(row["mechanism"])
                bf = QFont()
                bf.setPointSize(_BADGE_FONT_SIZE)
                badge.setFont(bf)
                badge.setStyleSheet(
                    "color: #94A3B8; background: rgba(30,35,50,200);"
                    " border-radius: 4px; padding: 1px 4px;"
                )
                layout.addWidget(badge)

            # latency
            if row["latency_ms"] is not None:
                lat_colors = {"green": "#34D399", "amber": "#FBBF24", "red": "#F46378"}
                lat_lbl = QLabel(row["latency_str"])
                lf = QFont()
                lf.setPointSize(_BADGE_FONT_SIZE)
                lat_lbl.setFont(lf)
                lat_lbl.setStyleSheet(f"color: {lat_colors.get(row['latency_cls'], '#94A3B8')};")
                layout.addWidget(lat_lbl)

            # undo chip
            if row["undo_id"] and on_undo is not None:
                undo_id = row["undo_id"]
                btn = QPushButton("Undo")
                btn.setFixedSize(52, 22)
                btn.setStyleSheet(
                    "QPushButton { color: #22D3EE; background: rgba(34,211,238,30);"
                    " border: 1px solid #22D3EE; border-radius: 4px; font-size: 10px; }"
                    " QPushButton:hover { background: rgba(34,211,238,60); }"
                )
                btn.clicked.connect(lambda _, uid=undo_id: on_undo(uid))
                layout.addWidget(btn)

    class HistoryWidget(QWidget):
        """Frosted scrollable history panel showing telemetry events.

        Usage::

            hw = HistoryWidget(on_undo=router.undo)
            hw.toggle()          # keyboard shortcut shows/hides
            hw.show_log()        # explicit open
            hw.hide()            # explicit close

        P2 wires a keyboard shortcut or ``_Bridge`` signal to ``toggle``.
        ``on_undo(undo_id:str)`` is called when the user taps an Undo chip.
        """

        def __init__(self, *, on_undo: Optional[Callable[[str], None]] = None) -> None:
            super().__init__()
            self._on_undo = on_undo
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            # NOT WA_TransparentForMouseEvents — this panel is interactive.

            self.setFixedWidth(_PANEL_W)
            self._pinned = False

        # -- public API ------------------------------------------------------ #

        def toggle(self) -> None:
            """Toggle visibility: if hidden → show_log; if visible → hide."""
            if self.isVisible():
                self.hide()
            else:
                self.show_log()

        def show_log(self) -> None:
            """Read telemetry JSONL (lazy) and populate rows, then show panel."""
            events = self._load_events()
            self._populate(events)
            self._position()
            self.show()
            self.raise_()
            self._pin_once()

        def hide(self) -> None:  # noqa: A003
            super().hide()

        # -- internals ------------------------------------------------------- #

        def _load_events(self) -> list[dict]:
            try:
                from curby_jarvis.telemetry import read_events
                return read_events(50)
            except Exception:
                return []

        def _populate(self, events: list[dict]) -> None:
            """Clear and rebuild the row list from events (most recent last)."""
            # Remove existing layout content
            old_layout = self.layout()
            if old_layout is not None:
                while old_layout.count():
                    item = old_layout.takeAt(0)
                    if item and item.widget():
                        item.widget().deleteLater()
                QWidget().setLayout(old_layout)  # detach + discard

            outer = QVBoxLayout(self)
            outer.setContentsMargins(_PANEL_PAD, _PANEL_PAD, _PANEL_PAD, _PANEL_PAD)
            outer.setSpacing(0)

            # Header
            header = QLabel("Action History")
            hf = QFont()
            hf.setPointSize(12)
            hf.setWeight(QFont.Weight.Bold)
            header.setFont(hf)
            header.setStyleSheet("color: #22D3EE;")
            outer.addWidget(header)

            # Scroll area
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet("background: transparent;")
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

            container = QWidget()
            container.setStyleSheet("background: transparent;")
            vbox = QVBoxLayout(container)
            vbox.setContentsMargins(0, 4, 0, 0)
            vbox.setSpacing(2)

            if not events:
                empty = QLabel("No events yet.")
                empty.setStyleSheet("color: #64748B;")
                vbox.addWidget(empty)
            else:
                for ev in reversed(events):
                    row = format_row(ev)
                    row_w = _RowWidget(row, self._on_undo)
                    row_w.setStyleSheet("background: rgba(20,22,32,120); border-radius: 6px;")
                    vbox.addWidget(row_w)

            vbox.addStretch(1)
            scroll.setWidget(container)
            outer.addWidget(scroll, stretch=1)

            self.setFixedHeight(min(_PANEL_H, len(events) * (_ROW_H + 4) + 60 + 2 * _PANEL_PAD))

        def _position(self) -> None:
            """Right-anchor just inside the primary screen, vertically centred."""
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
            x = sw - _PANEL_W - 20
            y = (sh - self.height()) // 2
            self.move(x, y)

        def _pin_once(self) -> None:
            if not self._pinned:
                try:
                    from ..macwin import make_always_visible
                    make_always_visible(self, click_through=False)
                except Exception:
                    pass
                self._pinned = True

        def paintEvent(self, _event) -> None:  # noqa: N802
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r, g, b = _BG_RGB
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, self.width(), self.height()), 12.0, 12.0)
            p.fillPath(path, QBrush(QColor(r, g, b, _BG_ALPHA)))
            br, bg_, bb = _BORDER_RGB
            p.setPen(QPen(QColor(br, bg_, bb, _BORDER_ALPHA), 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 12.0, 12.0)

else:

    class HistoryWidget:  # type: ignore[no-redef]
        """Stub for headless environments; raises on construction."""

        def __init__(self, *_, **__):
            raise RuntimeError(
                "HistoryWidget requires PyQt6 + a display. "
                "The pure helper format_row is available without Qt."
            )


__all__ = ["HEADLESS", "format_row", "HistoryWidget"]

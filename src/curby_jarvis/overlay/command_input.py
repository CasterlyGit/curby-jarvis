"""On-screen command-input pane (UI-15) — type a command into the HUD itself.

WHY: ``--text`` mode currently reads commands from *stdin*, so the only way to
drive the controller without a mic is to type into the terminal that launched it.
That's invisible once the overlays are up. This pane puts a frosted input field
on screen — bottom-centre, above the Dock — so the user can type a command and
press Enter while looking at the HUD. Submitted text is handed to the exact same
entry point the voice listener and stdin reader use (``_on_voice_utterance``), so
routing, the confirm gate, barge-in, the phase machine and execution are all
identical to a spoken phrase.

Unlike every other overlay surface (which are click-through, focus-refusing HUD
chrome), this pane MUST accept keyboard focus. On macOS it asks the underlying
NSPanel for non-activating key status via ``macwin.allow_key_focus`` so the user
can type without the app stealing focus / bouncing the Dock.

HEADLESS CONTRACT: importing this module must NOT touch PyQt6 or any display.
All Qt symbols are imported lazily. HEADLESS is True when PyQt6 is absent. The
pure helper ``normalize_command`` is always importable and testable without Qt.
"""
from __future__ import annotations

from typing import Callable, Optional

# ---- headless probe ---------------------------------------------------------
try:  # pragma: no cover
    import PyQt6  # noqa: F401

    HEADLESS = False
except Exception:
    HEADLESS = True

# ---- aesthetic constants (match caption.py / preview_card idiom) ------------
_PANE_W = 520          # input field width (logical px)
_PANE_H = 46           # input field height
_ABOVE_DOCK = 120      # gap from screen bottom to pane bottom
_RADIUS = 12

_FONT_SIZE = 16
_BG_RGB = (0x14, 0x16, 0x20)      # near-black, faint blue tint
_BG_ALPHA = 225
_BORDER_RGB = (0x22, 0xD3, 0xEE)  # cyan neon
_TEXT_RGB = (0xFF, 0xFF, 0xFF)
_PLACEHOLDER_RGB = (0x8A, 0x93, 0xA6)


# ---- pure helper (headless-safe) -------------------------------------------

def normalize_command(text: Optional[str]) -> str:
    """Trim a raw typed line into a dispatchable command, or '' to ignore.

    Mirrors the stdin reader's contract: strip surrounding whitespace; a blank
    or whitespace-only line yields '' (caller drops it). Pure, Qt-free, testable.
    """
    if not text:
        return ""
    return text.strip()


# ---- widget (Qt lazy) -------------------------------------------------------

if not HEADLESS:
    from PyQt6.QtWidgets import QWidget, QLineEdit, QApplication
    from PyQt6.QtCore import Qt, QRectF
    from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QPainterPath, QFont

    class CommandInputWidget(QWidget):
        """Frameless frosted command-input pane.

        Usage::

            inp = CommandInputWidget(on_submit=app._on_voice_utterance)
            inp.show_pane()     # position bottom-centre, take key focus

        ``on_submit`` is called with the normalized command string when the user
        presses Enter; the field then clears for the next command. Empty lines are
        ignored. Escape clears the field without submitting.
        """

        def __init__(self, on_submit: Callable[[str], None]) -> None:
            super().__init__()
            self._on_submit = on_submit
            self._pinned = False

            # A real (focusable) panel — NOT WindowDoesNotAcceptFocus, unlike the
            # click-through HUD surfaces; this pane has to receive keystrokes.
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

            self._edit = QLineEdit(self)
            self._edit.setPlaceholderText(
                "Type a command and press Enter — e.g. 'open Spotify', 'mute'"
            )
            self._edit.returnPressed.connect(self._submit)
            f = QFont()
            f.setPointSize(_FONT_SIZE)
            self._edit.setFont(f)
            # Transparent field: the custom paintEvent draws the frosted ground;
            # QLineEdit only supplies the editable text + caret.
            tr, tg, tb = _TEXT_RGB
            pr, pg, pb = _PLACEHOLDER_RGB
            self._edit.setStyleSheet(
                "QLineEdit{background:transparent;border:none;"
                f"color:rgb({tr},{tg},{tb});"
                f"selection-background-color:rgba({_BORDER_RGB[0]},"
                f"{_BORDER_RGB[1]},{_BORDER_RGB[2]},120);}}"
                f"QLineEdit::placeholder{{color:rgb({pr},{pg},{pb});}}"
            )

        # -- public API ------------------------------------------------------ #

        def show_pane(self) -> None:
            """Position the pane bottom-centre, raise it, and take key focus.

            Safe to call repeatedly: the one-time NSPanel level/space pinning runs
            once, but the raise + re-key runs every call so the pane comes back to
            the front after a command reactivates another app."""
            self._reposition()
            if not self.isVisible():
                self.show()
            self._pin_once()
            self.raise_()
            # Re-assert non-activating key status every show (idempotent): without
            # this, a pane that lost key after a dispatch never reclaims keystrokes.
            try:
                from ..macwin import allow_key_focus
                allow_key_focus(self)
            except Exception:
                pass
            self._edit.setFocus(Qt.FocusReason.OtherFocusReason)
            self.update()

        # -- internals ------------------------------------------------------- #

        def _submit(self) -> None:
            cmd = normalize_command(self._edit.text())
            self._edit.clear()
            if not cmd:
                return
            try:
                self._on_submit(cmd)
            except Exception:
                # A bad dispatch must never kill the input pane (mirrors the stdin
                # reader's per-line guard) — the field is already cleared, ready
                # for the next command.
                pass
            # Executing a command can reactivate another app (e.g. a media key
            # hands focus back to the foreground app), and a non-activating panel
            # that loses key status orders itself out. Re-assert the pane after
            # every dispatch so it stays on screen, ready for the next command.
            self.show_pane()

        def _reposition(self) -> None:
            screen = QApplication.primaryScreen()
            geo = screen.availableGeometry() if screen else None
            if geo is not None:
                x = geo.x() + (geo.width() - _PANE_W) // 2
                y = geo.y() + geo.height() - _PANE_H - _ABOVE_DOCK
            else:  # pragma: no cover - no screen
                x, y = 200, 200
            self.setGeometry(x, y, _PANE_W, _PANE_H)
            pad = 16
            self._edit.setGeometry(pad, 0, _PANE_W - 2 * pad, _PANE_H)

        def _pin_once(self) -> None:
            if self._pinned:
                return
            try:
                from ..macwin import make_always_visible, allow_key_focus
                # NOT click_through: the pane must take clicks + keystrokes.
                make_always_visible(self, click_through=False)
                allow_key_focus(self)
            except Exception:
                pass
            self._pinned = True

        def keyPressEvent(self, event) -> None:  # noqa: N802
            if event.key() == Qt.Key.Key_Escape:
                self._edit.clear()
                return
            super().keyPressEvent(event)

        def paintEvent(self, _event) -> None:  # noqa: N802
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

            r, g, b = _BG_RGB
            path = QPainterPath()
            path.addRoundedRect(
                QRectF(0, 0, self.width(), self.height()), _RADIUS, _RADIUS
            )
            p.fillPath(path, QBrush(QColor(r, g, b, _BG_ALPHA)))

            br, bg2, bb = _BORDER_RGB
            p.setPen(QPen(QColor(br, bg2, bb, 170), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(
                QRectF(0.6, 0.6, self.width() - 1.2, self.height() - 1.2),
                _RADIUS, _RADIUS,
            )

else:

    class CommandInputWidget:  # type: ignore[no-redef]
        """Stub for headless environments; raises on construction."""

        def __init__(self, *_, **__):
            raise RuntimeError(
                "CommandInputWidget requires PyQt6 + a display. "
                "The pure helper normalize_command is available without Qt."
            )


__all__ = ["HEADLESS", "normalize_command", "CommandInputWidget"]

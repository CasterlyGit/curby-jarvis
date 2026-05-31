"""Per-display deixis calibration: normalized fingertip -> logical screen pixels.

The hand-signal daemon broadcasts the index fingertip as a normalized [0,1] point
in the *camera* frame. That maps onto the screen only after a per-display affine
correction: the camera's field of view, the user's seating angle, and which display
they're pointing at all distort the raw normalized point. `Calibration` solves that
2D affine from four corner samples (least-squares via numpy) and applies it in
`map()`. With no calibration on disk it falls back to a plain identity stretch of
[0,1]^2 into the primary display's logical geometry — good enough to point roughly,
and the overlay's confirm step covers the slop.

Coordinate convention (matches curby's screen_capture.py): all screen coords are Qt
LOGICAL pixels — the same space as QWidget.move(), QCursor.pos(), CGEvent, and the
overlay painter — so one mapped point is both clickable and paintable. QScreen
geometry offsets are honored so a point on a secondary display maps correctly.

Headless by design: the affine math (fit + map) is pure numpy and unit-testable with
NO Qt. Only display-geometry lookup (`screen_for_point`, the identity fallback's size,
and the per-display UUID key) touches Qt, and that import is lazy. On a machine with no
display we use a configurable default size so the math still resolves.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

# Default logical screen size when Qt is unavailable (e.g. CI / headless tests).
# Matches the dev machine's primary display; only used for the identity fallback.
DEFAULT_SCREEN = (1512, 982)

CALIB_PATH = Path.home() / ".curby" / "deixis_calib.json"

# A calibration fit needs the four screen CORNERS pointed at. We key the expected
# normalized->target pairing by corner name so callers can collect samples in any
# order. Targets are (nx_target, ny_target) in [0,1] display space.
CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")
_CORNER_TARGET = {
    "top_left": (0.0, 0.0),
    "top_right": (1.0, 0.0),
    "bottom_left": (0.0, 1.0),
    "bottom_right": (1.0, 1.0),
}


def _affine_identity_into(left: float, top: float, w: float, h: float) -> np.ndarray:
    """Affine mapping [0,1]^2 -> the rect at (left,top) sized (w,h), as a 2x3 matrix.

    Row-form: [sx, sy] = M @ [nx, ny, 1]. With no rotation/shear this is just a
    scale + translate, but we store it in the same 2x3 shape a fitted affine uses
    so map() has a single code path.
    """
    return np.array(
        [[float(w), 0.0, float(left)],
         [0.0, float(h), float(top)]],
        dtype=float,
    )


def _fit_affine(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares 2D affine: find M (2x3) minimizing ||M @ [src;1] - dst||.

    `src` is (N,2) normalized points, `dst` is (N,2) screen px. With N>=3 this is
    overdetermined and numpy's lstsq gives the best-fit affine (handles the slight
    non-coplanarity of four hand-pointed corners). Returns a 2x3 matrix in the same
    row-form as the identity fallback.
    """
    n = src.shape[0]
    # Design matrix A = [nx, ny, 1]; solve A @ P = dst for P (3x2), then transpose.
    a = np.hstack([src, np.ones((n, 1))])           # (N,3)
    p, *_ = np.linalg.lstsq(a, dst, rcond=None)     # (3,2)
    return p.T                                       # (2,3): [sx;sy] = M @ [nx;ny;1]


@dataclass
class Calibration:
    """Maps normalized fingertip points to logical screen pixels for one display.

    `matrix` is the active 2x3 affine. If None, `map()` lazily builds an identity
    stretch into the primary display geometry (or DEFAULT_SCREEN headless).
    `display_uuid` keys saved calibrations so a multi-monitor user keeps a fit per
    screen; `default_size` overrides the headless fallback dimensions for testing.
    """

    matrix: Optional[np.ndarray] = None
    display_uuid: str = "primary"
    default_size: tuple = DEFAULT_SCREEN
    _origin: tuple = field(default=(0.0, 0.0), repr=False)  # logical (left, top) of target display

    # ---- core math (pure; no Qt) -------------------------------------------

    def fit(self, corner_samples: dict | list) -> np.ndarray:
        """Solve + store the affine from four corner (nx,ny)->(screen_x,screen_y) samples.

        Accepts either:
          - dict keyed by CORNERS name -> {"norm": (nx,ny), "screen": (sx,sy)}
            (or the looser {"nx":..,"ny":..,"sx":..,"sy":..}), or
          - a list of ((nx,ny),(sx,sy)) pairs (any length >= 3).
        Returns the fitted 2x3 matrix and sets it as active. Raises ValueError on
        too few points so a half-finished calibration can't silently install a
        degenerate transform.
        """
        src, dst = [], []
        if isinstance(corner_samples, dict):
            for _name, s in corner_samples.items():
                norm, screen = _coerce_sample(s)
                src.append(norm)
                dst.append(screen)
        else:
            for pair in corner_samples:
                norm, screen = _coerce_sample(pair)
                src.append(norm)
                dst.append(screen)

        if len(src) < 3:
            raise ValueError(f"need >=3 corner samples to fit an affine, got {len(src)}")

        m = _fit_affine(np.asarray(src, dtype=float), np.asarray(dst, dtype=float))
        self.matrix = m
        return m

    def map(self, nx: float, ny: float) -> tuple:
        """Apply the calibration: normalized (nx,ny) -> logical (screen_x, screen_y).

        Uncalibrated -> lazily resolve the primary display geometry (Qt if present,
        else DEFAULT_SCREEN) and stretch [0,1]^2 into it. The returned point is in
        the SAME logical-pixel space the overlay, AX, and CGEvent use.
        """
        m = self.matrix
        if m is None:
            m = self._identity_matrix()
        v = np.array([float(nx), float(ny), 1.0], dtype=float)
        out = m @ v
        return (float(out[0]), float(out[1]))

    def _identity_matrix(self) -> np.ndarray:
        """Build (and cache) the identity-into-primary affine. Lazy Qt for geometry."""
        left, top, w, h = self._primary_geometry()
        self._origin = (float(left), float(top))
        m = _affine_identity_into(left, top, w, h)
        self.matrix = m  # cache so repeated map() calls don't re-probe Qt
        return m

    # ---- display geometry (lazy Qt; falls back headless) -------------------

    def _primary_geometry(self) -> tuple:
        """(left, top, w, h) of the primary display in logical px. Lazy Qt; else default."""
        geo = _primary_geometry_qt()
        if geo is not None:
            return geo
        w, h = self.default_size
        return (0.0, 0.0, float(w), float(h))

    # ---- persistence -------------------------------------------------------

    def save(self, path: Path | str | None = None) -> Path:
        """Persist this calibration as JSON keyed by display UUID under ~/.curby/.

        Merges into any existing file so per-display fits coexist. A None matrix is
        saved as null (the identity fallback is implied at load time).
        """
        p = Path(path) if path is not None else CALIB_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        store = {}
        if p.exists():
            try:
                store = json.loads(p.read_text())
            except Exception:
                store = {}  # corrupt file -> start fresh rather than crash calibration
        store[self.display_uuid] = {
            "matrix": None if self.matrix is None else np.asarray(self.matrix).tolist(),
            "default_size": list(self.default_size),
        }
        p.write_text(json.dumps(store, indent=2))
        return p

    @classmethod
    def load(
        cls,
        path: Path | str | None = None,
        display_uuid: str | None = None,
        default_size: tuple = DEFAULT_SCREEN,
    ) -> "Calibration":
        """Load the calibration for `display_uuid` (default: current primary display).

        Missing file / missing key -> an uncalibrated Calibration (identity fallback).
        Never raises on a bad/absent file: a broken calib must degrade to point-rough,
        not break the controller.
        """
        p = Path(path) if path is not None else CALIB_PATH
        uuid = display_uuid if display_uuid is not None else _primary_display_uuid()
        store = {}
        if p.exists():
            try:
                store = json.loads(p.read_text())
            except Exception:
                store = {}
        entry = store.get(uuid) or {}
        raw = entry.get("matrix")
        matrix = None if raw is None else np.asarray(raw, dtype=float)
        size = tuple(entry.get("default_size") or default_size)
        return cls(matrix=matrix, display_uuid=uuid, default_size=size)

    # ---- multi-display routing (lazy Qt) -----------------------------------

    def screen_for_point(self, x: float, y: float):
        """Return the QScreen whose logical geometry contains (x,y), else primary/None.

        Lazy Qt: returns None when Qt or a running QApplication is unavailable, so
        headless callers can guard on it without importing PyQt6.
        """
        return _screen_for_point_qt(x, y)


# ---- module-level coercion + lazy-Qt helpers --------------------------------


def _coerce_sample(s) -> tuple:
    """Normalize one sample into ((nx,ny),(sx,sy)). Accepts several shapes.

    Supported:
      - ((nx,ny),(sx,sy))         pair tuple/list
      - {"norm":(nx,ny),"screen":(sx,sy)}
      - {"nx":..,"ny":..,"sx":..,"sy":..}
    """
    if isinstance(s, dict):
        if "norm" in s and "screen" in s:
            n, sc = s["norm"], s["screen"]
            return (float(n[0]), float(n[1])), (float(sc[0]), float(sc[1]))
        if {"nx", "ny", "sx", "sy"} <= set(s):
            return (float(s["nx"]), float(s["ny"])), (float(s["sx"]), float(s["sy"]))
        raise ValueError(f"unrecognized calibration sample dict: {s!r}")
    # assume a ((nx,ny),(sx,sy)) pair
    norm, screen = s
    return (float(norm[0]), float(norm[1])), (float(screen[0]), float(screen[1]))


def _qt_screens():
    """Return (QApplication.instance().screens()) or None if Qt/app unavailable.

    WHY no app creation: instantiating QApplication off the main thread or in a
    headless process is unsafe and can crash. We only read geometry from an app the
    overlay already created; absent one, callers fall back to defaults.
    """
    try:
        from PyQt6.QtWidgets import QApplication  # lazy: keeps module headless
    except Exception:
        return None
    app = QApplication.instance()
    if app is None:
        return None
    try:
        return app.screens()
    except Exception:
        return None


def _primary_geometry_qt() -> Optional[tuple]:
    """(left, top, w, h) of the primary QScreen, or None if Qt unavailable."""
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception:
        return None
    app = QApplication.instance()
    if app is None:
        return None
    try:
        scr = app.primaryScreen()
        g = scr.geometry()
        return (float(g.x()), float(g.y()), float(g.width()), float(g.height()))
    except Exception:
        return None


def _primary_display_uuid() -> str:
    """Stable UUID for the primary display, or 'primary' if it can't be read.

    Uses Quartz CGDisplayCreateUUIDFromDisplayID against the main display so a saved
    calibration re-binds to the same physical screen across reconnects. Lazy import;
    any failure degrades to the literal 'primary' key.
    """
    try:
        import Quartz
        did = Quartz.CGMainDisplayID()
        cf_uuid = Quartz.CGDisplayCreateUUIDFromDisplayID(did)
        if cf_uuid is None:
            return "primary"
        s = Quartz.CFUUIDCreateString(None, cf_uuid)
        return str(s) if s else "primary"
    except Exception:
        return "primary"


def _screen_for_point_qt(x: float, y: float):
    """QScreen containing logical (x,y); primary if none contain it; None headless."""
    screens = _qt_screens()
    if not screens:
        return None
    try:
        from PyQt6.QtCore import QPoint
        pt = QPoint(int(round(x)), int(round(y)))
        for scr in screens:
            if scr.geometry().contains(pt):
                return scr
        from PyQt6.QtWidgets import QApplication
        return QApplication.instance().primaryScreen()
    except Exception:
        return None


__all__ = ["Calibration", "CORNERS", "CALIB_PATH", "DEFAULT_SCREEN"]

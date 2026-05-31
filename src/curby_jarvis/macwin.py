"""macOS NSWindow shim — pin a Qt overlay above every app, on every space.

Vendored from curby's verified src/mac_window.py (the NSStatusWindowLevel + all-
spaces + hide-on-deactivate-off treatment that keeps curby's overlays visible).
Kept self-contained so curby-jarvis ships without a curby checkout. Safe on every
platform: no-ops on non-darwin or if pyobjc is missing.
"""
from __future__ import annotations

import ctypes
import sys

_LEVEL_STATUS_BAR = 25                 # NSStatusWindowLevel — floats above app windows
_BEHAVIOR_CAN_JOIN_ALL_SPACES = 1 << 0
_BEHAVIOR_STATIONARY = 1 << 4


def make_always_visible(widget, *, click_through: bool = False) -> None:
    """Pin a Qt widget so it floats above every app on every space.

    `click_through=True` also tells the underlying NSPanel to ignore mouse events
    so clicks pass through to the app below — required for any overlay sized large
    enough to sit under the cursor (the deixis reticle tracks the fingertip).
    Qt's WA_TransparentForMouseEvents alone is NOT enough on macOS.
    """
    if sys.platform != "darwin":
        return
    try:
        import objc

        nsview_ptr = int(widget.winId())
        if not nsview_ptr:
            return
        nsview = objc.objc_object(c_void_p=ctypes.c_void_p(nsview_ptr))
        nswindow = nsview.window()
        if nswindow is None:
            return
        nswindow.setLevel_(_LEVEL_STATUS_BAR)
        nswindow.setCollectionBehavior_(
            _BEHAVIOR_CAN_JOIN_ALL_SPACES | _BEHAVIOR_STATIONARY
        )
        try:
            nswindow.setHidesOnDeactivate_(False)
        except Exception:
            pass
        if click_through:
            try:
                nswindow.setIgnoresMouseEvents_(True)
            except Exception:
                pass
    except Exception:
        pass

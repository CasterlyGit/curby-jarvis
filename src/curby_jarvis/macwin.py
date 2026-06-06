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


_ACTIVATION_POLICY_ACCESSORY = 1  # NSApplicationActivationPolicyAccessory


def set_accessory_policy() -> None:
    """Run as an accessory app: overlays float without a Dock icon or focus theft.

    No-op on non-darwin or if pyobjc/AppKit is missing. The frosted confirm card
    is still fully clickable — accessory apps may present panels and receive
    clicks; only the activation policy (Dock presence / menu bar ownership)
    changes, which is exactly right for an overlay-only controller.
    """
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication

        NSApplication.sharedApplication().setActivationPolicy_(
            _ACTIVATION_POLICY_ACCESSORY
        )
    except Exception:
        pass


def allow_key_focus(widget) -> None:
    """Let an always-visible accessory panel become the key window (take keystrokes).

    An accessory app's overlay panels float without stealing focus, which is right
    for click-through HUD surfaces — but a text-input pane must receive keyboard
    events. NSPanel refuses to become key by default; flipping the underlying
    panel's style so it permits key status (and asking AppKit to make it key) is
    what lets the user type into the pane without activating a Dock app.

    No-op off darwin / without a real window server / without pyobjc.
    """
    if sys.platform != "darwin":
        return
    try:
        from PyQt6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is not None and app.platformName() in ("offscreen", "minimal", ""):
            return
    except Exception:
        pass
    try:
        import objc

        nsview_ptr = int(widget.winId())
        if not nsview_ptr:
            return
        nsview = objc.objc_object(c_void_p=ctypes.c_void_p(nsview_ptr))
        nswindow = nsview.window()
        if nswindow is None:
            return
        # NSWindowStyleMaskNonactivatingPanel (1<<7) lets the panel take key/mouse
        # input without activating the app (no Dock bounce, no focus theft).
        try:
            mask = int(nswindow.styleMask()) | (1 << 7)
            nswindow.setStyleMask_(mask)
        except Exception:
            pass
        # setStyleMask_ resets hidesOnDeactivate back to the default (True) for a
        # panel — which makes the pane order out the instant another app activates
        # (e.g. right after a media-key command). Re-assert it AFTER the mask flip,
        # and keep the panel on every space so it persists across the dispatch.
        try:
            nswindow.setHidesOnDeactivate_(False)
        except Exception:
            pass
        try:
            nswindow.setLevel_(_LEVEL_STATUS_BAR)
            nswindow.setCollectionBehavior_(
                _BEHAVIOR_CAN_JOIN_ALL_SPACES | _BEHAVIOR_STATIONARY
            )
        except Exception:
            pass
        try:
            nswindow.orderFrontRegardless()
        except Exception:
            pass
    except Exception:
        pass


def make_always_visible(widget, *, click_through: bool = False) -> None:
    """Pin a Qt widget so it floats above every app on every space.

    `click_through=True` also tells the underlying NSPanel to ignore mouse events
    so clicks pass through to the app below — required for any overlay sized large
    enough to sit under the cursor (the deixis reticle tracks the fingertip).
    Qt's WA_TransparentForMouseEvents alone is NOT enough on macOS.
    """
    if sys.platform != "darwin":
        return
    # No real window server (offscreen/minimal Qt platform, e.g. CI): winId() does
    # not back a valid NSView, so the AppKit calls below would dereference garbage
    # and can segfault. There's nothing to pin without a compositor — bail.
    try:
        from PyQt6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is not None and app.platformName() in ("offscreen", "minimal", ""):
            return
    except Exception:
        pass
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

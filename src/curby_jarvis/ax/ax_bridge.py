"""Accessibility bridge — hit-test, menu-bar resolve, semantic press, AXFrame reads.

EVERY AX call blocks on the target app's main run loop, so each is wrapped in a
thread+timeout watchdog: on timeout we surface 'ax_timeout' and let the caller
fall to the CGEvent floor rather than freezing the whole point-and-say path.

All pyobjc imports are lazy so this module imports headless (CI, no permission).
The functions degrade to None/False when pyobjc or the Accessibility grant is
missing, so connectors never crash — they fall through the chain.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_TIMEOUT = 0.15
MENU_TIMEOUT = 0.4


@dataclass
class AXElementInfo:
    role: str = ""
    title: str = ""
    value: str = ""
    frame: Optional[tuple] = None   # (x, y, w, h) logical px
    pid: int = 0
    app_name: str = ""
    has_press: bool = False
    ref: Any = None                 # the AXUIElementRef, for press()

    def label(self) -> str:
        """Human one-liner for the preview card."""
        bits = [b for b in (self.role.replace("AX", ""), self.title or self.value) if b]
        head = " ".join(bits).strip() or "element"
        return f"{head} in {self.app_name}" if self.app_name else head


def _with_timeout(fn, timeout: float, default=None):
    """Run fn() on a daemon thread; return its result, or `default` on timeout.
    AX calls can hang on a wedged app — never let one freeze the caller."""
    box = {"v": default, "done": False}

    def run():
        try:
            box["v"] = fn()
        except Exception:
            box["v"] = default
        finally:
            box["done"] = True

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return box["v"] if box["done"] else default


def ax_available() -> bool:
    """True if this process is a trusted Accessibility client (TCC granted)."""
    def _():
        from ApplicationServices import AXIsProcessTrusted
        return bool(AXIsProcessTrusted())
    return bool(_with_timeout(_, 0.5, default=False))


def _copy_attr(elem, attr):
    from ApplicationServices import AXUIElementCopyAttributeValue
    err, val = AXUIElementCopyAttributeValue(elem, attr, None)
    return None if err else val


def _s(v) -> str:
    try:
        return "" if v is None else str(v)
    except Exception:
        return ""


def _has_action(elem, action) -> bool:
    try:
        from ApplicationServices import AXUIElementCopyActionNames
        err, names = AXUIElementCopyActionNames(elem, None)
        return bool(err == 0 and names and action in names)
    except Exception:
        return False


def _owner(elem):
    try:
        from ApplicationServices import AXUIElementGetPid
        err, pid = AXUIElementGetPid(elem, None)
        if err:
            return 0, ""
        from AppKit import NSRunningApplication
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return int(pid), (_s(app.localizedName()) if app else "")
    except Exception:
        return 0, ""


def _frame_of(elem) -> Optional[tuple]:
    try:
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXValueGetValue,
            kAXPositionAttribute,
            kAXSizeAttribute,
            kAXValueCGPointType,
            kAXValueCGSizeType,
        )
        perr, pos = AXUIElementCopyAttributeValue(elem, kAXPositionAttribute, None)
        serr, size = AXUIElementCopyAttributeValue(elem, kAXSizeAttribute, None)
        if perr or serr or pos is None or size is None:
            return None
        okp, pt = AXValueGetValue(pos, kAXValueCGPointType, None)
        oks, sz = AXValueGetValue(size, kAXValueCGSizeType, None)
        if not (okp and oks):
            return None
        return (float(pt.x), float(pt.y), float(sz.width), float(sz.height))
    except Exception:
        return None


def element_at(x: float, y: float, timeout: float = DEFAULT_TIMEOUT) -> Optional[AXElementInfo]:
    """Hit-test the system-wide AX tree at logical (x, y). Returns structured info
    (role/title/frame/pid/has_press) or None on miss/timeout/no-permission."""
    def _():
        from ApplicationServices import (
            AXUIElementCopyElementAtPosition,
            AXUIElementCreateSystemWide,
            kAXDescriptionAttribute,
            kAXPressAction,
            kAXRoleAttribute,
            kAXTitleAttribute,
            kAXValueAttribute,
        )
        sw = AXUIElementCreateSystemWide()
        err, elem = AXUIElementCopyElementAtPosition(sw, float(x), float(y), None)
        if err or elem is None:
            return None
        info = AXElementInfo(ref=elem)
        info.role = _s(_copy_attr(elem, kAXRoleAttribute))
        info.title = _s(_copy_attr(elem, kAXTitleAttribute)) or _s(_copy_attr(elem, kAXDescriptionAttribute))
        info.value = _s(_copy_attr(elem, kAXValueAttribute))
        info.frame = _frame_of(elem)
        info.has_press = _has_action(elem, kAXPressAction)
        info.pid, info.app_name = _owner(elem)
        return info

    return _with_timeout(_, timeout)


def press(info_or_ref, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Semantic AXPress on a resolved element — no cursor move, works on occluded
    elements. Returns False if there is no press action (caller falls to CGEvent)."""
    ref = getattr(info_or_ref, "ref", info_or_ref)
    if ref is None:
        return False

    def _():
        from ApplicationServices import AXUIElementPerformAction, kAXPressAction
        return AXUIElementPerformAction(ref, kAXPressAction) == 0

    return bool(_with_timeout(_, timeout, default=False))


def frontmost_pid_name():
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return 0, ""
        return int(app.processIdentifier()), _s(app.localizedName())
    except Exception:
        return 0, ""


def menu_command(title_query: str, timeout: float = MENU_TIMEOUT) -> bool:
    """Walk the frontmost app's OWN menu bar (kAXMenuBarAttribute), fuzzy-match a
    command title, and AXPress it. Localization-proof and keystroke-free — the
    single most maintainable primitive in the controller."""
    def _():
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
            AXUIElementPerformAction,
            kAXChildrenAttribute,
            kAXMenuBarAttribute,
            kAXPressAction,
            kAXTitleAttribute,
        )
        pid, _name = frontmost_pid_name()
        if not pid:
            return False
        app = AXUIElementCreateApplication(pid)
        err, menubar = AXUIElementCopyAttributeValue(app, kAXMenuBarAttribute, None)
        if err or menubar is None:
            return False
        target = (title_query or "").strip().lower()
        if not target:
            return False
        stack = [menubar]
        seen = 0
        while stack and seen < 5000:
            seen += 1
            el = stack.pop()
            cerr, kids = AXUIElementCopyAttributeValue(el, kAXChildrenAttribute, None)
            if cerr or not kids:
                continue
            for k in kids:
                terr, title = AXUIElementCopyAttributeValue(k, kAXTitleAttribute, None)
                t = _s(title).strip().lower()
                if t and (t == target or target in t):
                    if AXUIElementPerformAction(k, kAXPressAction) == 0:
                        return True
                stack.append(k)
        return False

    return bool(_with_timeout(_, timeout, default=False))

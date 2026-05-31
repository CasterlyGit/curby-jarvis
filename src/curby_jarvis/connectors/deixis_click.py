"""DeixisClickConnector — the point-and-say effector (cost=4, the AX-press tier).

Handles the deictic verbs whose target is a screen coordinate the FusionBinder
already resolved from the gesture stream: click_at / move / drag, plus 'play THIS'
when it carries a pointer. Everything earlier in the chain (URL, media-key,
menubar) is target-by-name; this is target-by-POINT.

Resolution order, cheapest + safest first:
  1. preview(): hit-test ax_bridge.element_at(pointer) -> AXElementInfo. Fill the
     card's target_rect (the bracket) + gloss (info.label()) so the user sees
     EXACTLY what will be hit before confirming.
  2. execute(): prefer ax_bridge.press(info) — semantic, no cursor move, works on
     occluded/background elements. Only when there is no press action do we drop
     to the CGEvent click floor (which moves the real cursor).
  3. move/drag => cgevent.drag(pointer -> pointer2).
  4. AX miss (element_at -> None): optionally LABEL the pixels under the crosshair
     with a Tier-2 vision call so a blind click is at least named, and force
     confirm. Never silently blind-click.

Native imports (ax_bridge, cgevent, screen, anthropic) are lazy/at-module only
where pure; the OS work all lives behind ax_bridge/cgevent which are themselves
lazy. execute() NEVER raises — every failure comes back as ConnectorResult.
"""
from __future__ import annotations

import time
from typing import Optional

from . import Connector
from ..intent import (
    RISK_AMBIGUOUS,
    ConnectorResult,
    Intent,
    PreviewCard,
)

# Verbs this connector serves when they carry (or need) a resolved pointer.
_POINT_VERBS = {"click_at", "move", "drag"}

_MECH_AX_PRESS = "ax_press"
_MECH_CG_CLICK = "cgevent_click"
_MECH_CG_DRAG = "cgevent_drag"
_MECH_VISION = "vision_label"


class DeixisClickConnector(Connector):
    name = "deixis_click"
    cost = 4  # ax_press tier (see connectors/__init__ cost ladder)

    def __init__(self, *, vision: bool = True):
        # vision gate: when True, an AX miss triggers the Tier-2 label grab. Off
        # in tests / when no API key, so we degrade to forced-confirm blind click.
        self._vision = vision

    # -- routing --------------------------------------------------------------

    def _serves(self, intent: Intent) -> bool:
        if intent.verb in _POINT_VERBS:
            return True
        # 'play THIS' rides this connector only when it's a deictic point.
        if intent.verb == "play" and intent.needs_pointer:
            return True
        return False

    def can_handle(self, intent: Intent) -> float:
        if not self._serves(intent):
            return 0.0
        # High confidence once a pointer is bound; still claim the verb when it
        # isn't (so the chain doesn't fall to a name-based connector that can't
        # possibly resolve a deictic 'this') — but lower, and the card will be
        # ambiguous -> forced confirm 'point and confirm'.
        return 0.9 if intent.pointer is not None else 0.5

    def is_available(self, intent: Intent) -> bool:
        from ..ax import ax_bridge
        from ..ax.secure_input import secure_input_active
        return ax_bridge.ax_available() and not secure_input_active()

    # -- preview (no side effects) -------------------------------------------

    def _resolve(self, intent: Intent):
        """Hit-test the pointer -> AXElementInfo|None. Pure-ish: just an AX read."""
        if intent.pointer is None:
            return None
        from ..ax import ax_bridge
        x, y = intent.pointer
        return ax_bridge.element_at(float(x), float(y))

    def preview(self, intent: Intent) -> PreviewCard:
        title = self._title(intent)
        card = PreviewCard(title=title, mechanism=self.name, risk=intent.risk)

        if intent.pointer is None:
            # FusionBinder found no qualifying gesture sample -> stay ambiguous so
            # the overlay shows 'point and confirm'. Never invent a coordinate.
            card.risk = RISK_AMBIGUOUS
            card.gloss = "point at the target, then confirm"
            card.literal = "pointer: unresolved"
            return card

        card.literal = self._literal(intent)
        info = self._resolve(intent)
        if info is not None:
            card.gloss = info.label()
            card.target_rect = info.frame  # (x,y,w,h) for the bracket
            # mechanism the card advertises = what execute() will actually do
            card.mechanism = _MECH_AX_PRESS if info.has_press else _MECH_CG_CLICK
            if intent.verb in ("move", "drag"):
                card.mechanism = _MECH_CG_DRAG
            return card

        # AX miss: optionally label via vision so the user isn't confirming blind.
        card.mechanism = _MECH_CG_DRAG if intent.verb in ("move", "drag") else _MECH_CG_CLICK
        card.gloss = self._vision_label(intent) or "no element resolved here"
        card.risk = RISK_AMBIGUOUS  # force confirm: we're about to click unlabeled pixels
        return card

    # -- execute (watchdogged; never raises) ----------------------------------

    def execute(self, intent: Intent) -> ConnectorResult:
        t0 = time.time()
        try:
            from ..ax.secure_input import secure_input_active
            if secure_input_active():
                return ConnectorResult(ok=False, mechanism=self.name,
                                       error="secure_input_blocked")
            if intent.pointer is None:
                return ConnectorResult(ok=False, mechanism=self.name,
                                       error="unresolved_pointer",
                                       detail="no gesture sample bound; point and confirm")

            if intent.verb in ("move", "drag"):
                return self._do_drag(intent, t0)
            return self._do_click(intent, t0)
        except Exception as e:  # HARD RULE: never raise out of execute
            return ConnectorResult(ok=False, mechanism=self.name,
                                   error="exception", detail=repr(e),
                                   latency_ms=(time.time() - t0) * 1000.0)

    def _do_click(self, intent: Intent, t0: float) -> ConnectorResult:
        from ..ax import ax_bridge
        from .. import cgevent

        x, y = intent.pointer
        info = self._resolve(intent)

        # Prefer semantic AXPress: no cursor move, works on occluded elements.
        if info is not None and info.has_press:
            if ax_bridge.press(info):
                return ConnectorResult(ok=True, mechanism=_MECH_AX_PRESS,
                                       latency_ms=(time.time() - t0) * 1000.0)
            # press exposed but failed (timeout/wedge) -> fall to the CGEvent floor

        # CGEvent click floor. If we had no AX element at all, the preview already
        # forced confirm, so reaching here means the user OK'd a labeled/blind click.
        ok = cgevent.click(float(x), float(y))
        return ConnectorResult(
            ok=bool(ok),
            mechanism=_MECH_CG_CLICK,
            error="" if ok else "click_failed",
            latency_ms=(time.time() - t0) * 1000.0,
        )

    def _do_drag(self, intent: Intent, t0: float) -> ConnectorResult:
        from .. import cgevent

        x1, y1 = intent.pointer
        dest = intent.pointer2
        if dest is None:
            # 'move THIS THERE' with no second point bound -> can't drag safely.
            return ConnectorResult(ok=False, mechanism=_MECH_CG_DRAG,
                                   error="unresolved_pointer2",
                                   detail="destination point not bound",
                                   latency_ms=(time.time() - t0) * 1000.0)
        x2, y2 = dest
        ok = cgevent.drag(float(x1), float(y1), float(x2), float(y2))
        return ConnectorResult(
            ok=bool(ok),
            mechanism=_MECH_CG_DRAG,
            error="" if ok else "drag_failed",
            latency_ms=(time.time() - t0) * 1000.0,
        )

    # -- card text helpers ----------------------------------------------------

    def _title(self, intent: Intent) -> str:
        if intent.verb == "play":
            return "play THIS"
        if intent.verb == "click_at":
            return "click HERE"
        if intent.verb == "move":
            return "move THIS THERE"
        if intent.verb == "drag":
            return "drag THIS THERE"
        return intent.verb

    def _literal(self, intent: Intent) -> str:
        p = intent.pointer
        s = f"({p[0]:.0f}, {p[1]:.0f})" if p else "unresolved"
        if intent.verb in ("move", "drag") and intent.pointer2:
            q = intent.pointer2
            s += f" -> ({q[0]:.0f}, {q[1]:.0f})"
        return s

    # -- Tier-2 vision label (lazy; optional) ---------------------------------

    def _vision_label(self, intent: Intent) -> Optional[str]:
        """Grab pixels around the pointer and ask the model WHAT is at the
        crosshair, so an AX-miss click is named before the user confirms.

        Best-effort: returns None (and execute still forces confirm) if vision is
        disabled, capture deps/perm are missing, or the model/key is unavailable.
        Never raises.
        """
        if not self._vision or intent.pointer is None:
            return None
        try:
            import os
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return None
            from .. import screen
            if not screen.capture_available():
                return None
            x, y = intent.pointer
            img = screen.grab_region(float(x), float(y), radius=180)
            return self._ask_vision(img)
        except Exception:
            return None

    def _ask_vision(self, img) -> Optional[str]:
        """Single forced-tool-free haiku call: 'what is at the crosshair center?'
        Lazy anthropic import; short, cheap, label-only. None on any failure."""
        try:
            import base64
            import io
            from anthropic import Anthropic

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
            client = Anthropic()
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=40,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/png", "data": b64}},
                        {"type": "text", "text":
                            "A crosshair is at the exact center of this crop. In 5 words or "
                            "fewer, name the UI element under the crosshair center."},
                    ],
                }],
            )
            parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
            label = " ".join(parts).strip()
            return label or None
        except Exception:
            return None


__all__ = ["DeixisClickConnector"]

"""ComputerUseConnector — pixel-level escape hatch via Anthropic computer-use beta.

WHY: when every structured AX/menu/script route is exhausted or the target UI is
non-scriptable (Electron apps, games, legacy Java, remote-desktop surfaces), a
screenshot→action loop driven by the model can still land the action.  This is
the highest-cost connector (cost=11) and fires only when the agent loop
explicitly selects it (intent.args['pixel'] truthy) or as a last-resort for
agent_task.

The loop calls client.beta.messages.create with the 'computer-use-2025-11-24'
beta flag, then maps each computer-use action block onto the cgevent floor:
mouse_move / left_click / right_click / scroll / type / key.  Screenshots travel
as base64 in tool_result blocks.  A watchdog (max_steps) bounds the loop.

All native/network imports are LAZY so this module imports headless.  Every
path that can fail is wrapped — execute_streaming never raises.
"""
from __future__ import annotations

import base64
import os
import time
from typing import Callable, Optional

from ..intent import ConnectorResult, Intent, PreviewCard, ProgressEvent, RISK_AMBIGUOUS
from . import Connector


class ComputerUseConnector(Connector):
    """Drive any on-screen UI via the Anthropic computer-use beta (cost=11).

    Instantiate with a fake client_factory in tests; P2 injects nothing (defaults
    are safe). screen_size is auto-detected from Quartz at first use when None.
    """

    name = "computer_use"
    cost = 11
    use_breaker = True

    def __init__(
        self,
        *,
        client_factory: Optional[Callable] = None,
        model: str = "claude-sonnet-4-6",
        max_steps: int = 10,
        screen_size: Optional[tuple] = None,
    ):
        self._client_factory = client_factory
        self._model = model
        self._max_steps = max_steps
        self._screen_size = screen_size  # (width, height) logical px; None = auto

    # -------------------------------------------------------------------------
    # Connector protocol
    # -------------------------------------------------------------------------

    def can_handle(self, intent: Intent) -> float:
        # Also match when the agent loop selects this tool by name (verb ==
        # 'computer_use'), not just the explicit pixel/agent_task paths.
        if intent.args.get("pixel") or intent.verb in ("agent_task", "computer_use"):
            return 0.1
        return 0.0

    def is_available(self, intent: Intent) -> bool:
        # Need an API key or injected factory.
        if self._client_factory is None and not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        # Screen Recording permission best-effort check.
        try:
            from ..screen import capture_available
            if not capture_available():
                return False
        except Exception:
            pass  # if we can't check, allow and surface failure at execute time
        return self.breaker_allows()

    def preview(self, intent: Intent) -> PreviewCard:
        return PreviewCard(
            title="computer use",
            gloss=intent.raw_utterance or intent.target,
            mechanism=self.name,
            risk=RISK_AMBIGUOUS,
            literal="claude computer-use beta loop",
        )

    def tool_schema(self) -> dict:
        return {
            "name": self.name,
            "description": (
                "pixel-level control of any on-screen UI when no structured route exists"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "verb": {"type": "string"},
                    "target": {"type": "string"},
                    "args": {
                        "type": "object",
                        "properties": {
                            "pixel": {
                                "type": "boolean",
                                "description": "Set true to force pixel-level computer use.",
                            }
                        },
                    },
                },
                "required": ["verb"],
            },
        }

    def supports_streaming(self) -> bool:
        return True

    def execute(self, intent: Intent) -> ConnectorResult:
        return self.execute_streaming(intent, lambda e: None)

    def execute_streaming(
        self, intent: Intent, on_event: Callable[[ProgressEvent], None]
    ) -> ConnectorResult:
        """Run the computer-use screenshot→action loop.

        Each iteration: capture screen → send to model → map action → execute →
        send tool_result → repeat.  Bounded by max_steps.  Never raises.
        """
        t0 = time.monotonic()

        def _emit(text: str, kind: str = "status", pct: Optional[float] = None) -> None:
            try:
                on_event(ProgressEvent(phase="acting", text=text, pct=pct,
                                       mechanism=self.name, kind=kind))
            except Exception:
                pass

        try:
            client = self._get_client()
        except Exception as exc:
            return ConnectorResult(ok=False, mechanism=self.name,
                                   error="client_unavailable", detail=str(exc))

        w, h = self._get_screen_size()
        task = intent.raw_utterance or intent.target or intent.verb

        messages = [{"role": "user", "content": task}]
        steps_run = 0
        final_text = ""

        try:
            for step in range(self._max_steps):
                _emit(f"step {step + 1}/{self._max_steps}", kind="step",
                      pct=(step / self._max_steps))

                # Capture current screen state.
                screenshot_b64 = self._capture_screenshot()

                # Build the tools list for this API call.
                tools = [
                    {
                        "type": "computer_20251124",
                        "name": "computer",
                        "display_width_px": w,
                        "display_height_px": h,
                    }
                ]

                # Add the screenshot as a tool_result if this is a loop continuation.
                if step > 0:
                    # Append screenshot as new user message (tool_result for prev action).
                    messages.append({
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": self._last_tool_use_id,
                                "content": [
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": "image/png",
                                            "data": screenshot_b64,
                                        },
                                    }
                                ],
                            }
                        ],
                    })

                response = client.beta.messages.create(
                    model=self._model,
                    max_tokens=1024,
                    tools=tools,
                    messages=messages,
                    betas=["computer-use-2025-11-24"],
                )

                steps_run += 1

                # Collect text from response.
                for block in response.content:
                    if hasattr(block, "text"):
                        final_text = block.text

                if response.stop_reason == "end_turn":
                    # Task complete.
                    break

                if response.stop_reason != "tool_use":
                    break

                # Find the computer tool_use block.
                tool_block = None
                for block in response.content:
                    if getattr(block, "type", None) == "tool_use":
                        tool_block = block
                        break

                if tool_block is None:
                    break

                self._last_tool_use_id = tool_block.id
                action = tool_block.input

                _emit(f"action: {action.get('action', 'unknown')}", kind="tool_call")
                success = self._dispatch_action(action)
                _emit(
                    f"action {'ok' if success else 'failed'}: {action.get('action', '')}",
                    kind="tool_result",
                )

                # Build assistant turn with the tool call.
                messages.append({"role": "assistant", "content": response.content})

                if not success:
                    # On failure we still feed back the screenshot next iteration.
                    pass

            # Record breaker outcome best-effort.
            try:
                if self.breaker:
                    self.breaker.record_success()
            except Exception:
                pass

            latency_ms = (time.monotonic() - t0) * 1000
            return ConnectorResult(
                ok=True,
                mechanism=self.name,
                latency_ms=latency_ms,
                detail_text=final_text,
                steps=steps_run,
            )

        except Exception as exc:
            try:
                if self.breaker:
                    self.breaker.record_failure()
            except Exception:
                pass
            return ConnectorResult(
                ok=False,
                mechanism=self.name,
                error="computer_use_error",
                detail=str(exc),
                latency_ms=(time.monotonic() - t0) * 1000,
            )

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _get_client(self):
        """Return an Anthropic client; lazy import so headless import works."""
        if self._client_factory is not None:
            return self._client_factory()
        import anthropic  # lazy — not available in CI
        return anthropic.Anthropic()

    def _get_screen_size(self) -> tuple:
        """Return (width, height) in logical pixels; auto-detect via Quartz."""
        if self._screen_size is not None:
            return self._screen_size
        try:
            from Quartz import CGDisplayBounds, CGMainDisplayID
            bounds = CGDisplayBounds(CGMainDisplayID())
            return int(bounds.size.width), int(bounds.size.height)
        except Exception:
            return 1440, 900  # safe default

    def _capture_screenshot(self) -> str:
        """Return a base64-encoded PNG of the current screen, or a 1x1 blank."""
        try:
            from ..screen import grab_region
            w, h = self._get_screen_size()
            # Grab full screen by centering a radius large enough to cover it.
            radius = max(w, h)
            img = grab_region(w // 2, h // 2, radius=radius)
            import io
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()
        except Exception:
            # Return minimal 1x1 white PNG so the loop doesn't crash.
            _BLANK_PNG = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
                b"\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx"
                b"\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00"
                b"\x00IEND\xaeB`\x82"
            )
            return base64.b64encode(_BLANK_PNG).decode()

    def _dispatch_action(self, action: dict) -> bool:
        """Map a computer-use action dict to cgevent primitives. Returns success bool."""
        from .. import cgevent  # lazy — Quartz not available in CI

        act = action.get("action", "")
        try:
            if act == "mouse_move":
                x, y = action.get("coordinate", [0, 0])
                # CGEvent move: synthesize a mouseMoved event.
                try:
                    from Quartz import (
                        CGEventCreateMouseEvent,
                        kCGEventMouseMoved,
                        kCGMouseButtonLeft,
                    )
                    from Quartz import CGEventPost, kCGHIDEventTap
                    ev = CGEventCreateMouseEvent(
                        None, kCGEventMouseMoved, (float(x), float(y)), kCGMouseButtonLeft
                    )
                    CGEventPost(kCGHIDEventTap, ev)
                    return True
                except Exception:
                    return False

            elif act == "left_click":
                x, y = action.get("coordinate", [0, 0])
                return cgevent.click(float(x), float(y))

            elif act == "right_click":
                x, y = action.get("coordinate", [0, 0])
                return self._right_click(float(x), float(y))

            elif act == "double_click":
                x, y = action.get("coordinate", [0, 0])
                return cgevent.double_click(float(x), float(y))

            elif act == "scroll":
                x, y = action.get("coordinate", [0, 0])
                # First move cursor to scroll position.
                cgevent.click(float(x), float(y))
                dx = action.get("delta_x", 0)
                dy = action.get("delta_y", 0)
                return cgevent.scroll(dx, dy)

            elif act == "type":
                text = action.get("text", "")
                return cgevent.type_text(text)

            elif act == "key":
                key_combo = action.get("text", "")
                # Computer-use sends key names like "Return", "ctrl+c", etc.
                # Normalize to cgevent format (lowercase, + separated).
                normalized = self._normalize_key(key_combo)
                return cgevent.key(normalized)

            elif act == "screenshot":
                # Screenshot request — just return True; next iteration sends it.
                return True

            else:
                return False

        except Exception:
            return False

    @staticmethod
    def _right_click(x: float, y: float) -> bool:
        """Synthesize a right mouse-button click at (x, y)."""
        try:
            from Quartz import (
                CGEventCreateMouseEvent,
                kCGEventRightMouseDown,
                kCGEventRightMouseUp,
                kCGMouseButtonRight,
            )
            from Quartz import CGEventPost, kCGHIDEventTap
            pt = (x, y)
            CGEventPost(kCGHIDEventTap,
                        CGEventCreateMouseEvent(None, kCGEventRightMouseDown, pt, kCGMouseButtonRight))
            CGEventPost(kCGHIDEventTap,
                        CGEventCreateMouseEvent(None, kCGEventRightMouseUp, pt, kCGMouseButtonRight))
            return True
        except Exception:
            return False

    @staticmethod
    def _normalize_key(key_str: str) -> str:
        """Normalize computer-use key names to cgevent format.

        'Return' -> 'return', 'ctrl+c' -> 'ctrl+c', 'super+l' -> 'cmd+l'.
        """
        mapping = {
            "Return": "return",
            "Escape": "escape",
            "Tab": "tab",
            "BackSpace": "delete",
            "Delete": "delete",
            "space": "space",
            "super": "cmd",
            "Super": "cmd",
            "ctrl": "ctrl",
            "Ctrl": "ctrl",
            "alt": "opt",
            "Alt": "opt",
            "shift": "shift",
            "Shift": "shift",
        }
        parts = key_str.split("+")
        normalized = []
        for p in parts:
            normalized.append(mapping.get(p, p.lower()))
        return "+".join(normalized)


__all__ = ["ComputerUseConnector"]

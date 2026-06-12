"""Connector base class — the pluggable effector contract.

Every connector is independently buildable and headless-unit-testable against the
frozen Intent. OS-touching imports (pyobjc, subprocess, PyQt) are LAZY inside
methods so importing a connector never requires a display, camera, or permission.

The router orders the chain by (cost, -can_handle): cheapest confident wins.
Declared cost ranks:
    url=1, media_key=2, menubar_ax=3, ax_press=4, browser_osascript=6, llm=8, agent=10
"""
from __future__ import annotations

from ..intent import ConnectorResult, Intent, PreviewCard


class Connector:
    #: stable display name (also the mechanism tag in telemetry)
    name: str = "connector"
    #: declared cost rank — cheaper wins when confidence ties
    cost: int = 5

    def can_handle(self, intent: Intent) -> float:
        """0.0 if this connector cannot serve the verb, else a 0..1 confidence."""
        return 0.0

    def is_available(self, intent: Intent) -> bool:
        """Cheap correctness probe (scheme registered? app scriptable? AX trusted?
        not in Secure Input?) so a mis-declared cost surfaces instead of routing worse."""
        return True

    def preview(self, intent: Intent) -> PreviewCard:
        """Build the overlay card WITHOUT side effects."""
        return PreviewCard(title=intent.verb, mechanism=self.name, risk=intent.risk)

    def execute(self, intent: Intent) -> ConnectorResult:
        """Perform the action with an internal watchdog. Never raises — errors come
        back in ConnectorResult so the router can fall through to the next connector."""
        raise NotImplementedError

    # -- INF-01: self-description so the agent loop can expose this as a tool ---

    #: short verb list this connector serves (override for a precise tool schema)
    verbs: tuple = ()

    def tool_schema(self) -> dict:
        """An Anthropic tool definition describing this connector to the agent loop
        (INF-02). The default derives name/description from the class; connectors
        override to declare an exact `input_schema`. The agent loop turns a tool_use
        block back into an Intent via `intent_from_tool_input`."""
        desc = (self.__doc__ or self.name).strip().splitlines()[0]
        if self.verbs:
            desc = f"{desc} Verbs: {', '.join(self.verbs)}."
        return {
            "name": self.name,
            "description": desc,
            "input_schema": {
                "type": "object",
                "properties": {
                    "verb": {"type": "string", "description": "action verb, e.g. open/click_at/type"},
                    "target": {"type": "string", "description": "app name, element label, URL, or text"},
                    "args": {"type": "object", "description": "extra arguments (x, y, name, ...)"},
                },
                "required": ["verb"],
            },
        }

    # -- INF-01: optional streaming for long-running connectors ----------------

    def supports_streaming(self) -> bool:
        """True if this connector can emit ProgressEvents during execute_streaming.
        Default False — the router/task engine then just calls execute()."""
        return False

    def execute_streaming(self, intent: Intent, on_event) -> ConnectorResult:
        """Execute while emitting ProgressEvent objects to ``on_event(ev)``. The
        base implementation just runs execute() and reports start/done so callers
        can rely on the streaming surface universally. Never raises."""
        from ..intent import ProgressEvent
        try:
            on_event(ProgressEvent(phase="acting", text=f"{intent.verb} {intent.target}".strip(),
                                   mechanism=self.name, kind="step"))
        except Exception:
            pass
        res = self.execute(intent)
        try:
            on_event(ProgressEvent(phase="done" if res.ok else "error",
                                   text=res.detail_text or res.error or "done",
                                   mechanism=self.name, kind="tool_result"))
        except Exception:
            pass
        return res

    # -- INF-15: opt-in circuit breaker (network/flaky connectors set use_breaker) --

    #: set True on network-bound connectors to get failover protection
    use_breaker: bool = False

    @property
    def breaker(self):
        """Lazy per-connector CircuitBreaker, or None when use_breaker is False.
        Import is lazy so the contract stays headless and works before the
        circuit_breaker module is loaded."""
        if not self.use_breaker:
            return None
        if getattr(self, "_breaker", None) is None:
            from ..circuit_breaker import CircuitBreaker
            self._breaker = CircuitBreaker(name=self.name)
        return self._breaker

    def breaker_allows(self) -> bool:
        """True unless an attached breaker is open. Connectors that override
        is_available() and want failover call this explicitly."""
        b = self.breaker
        return True if b is None else b.allow()


def intent_from_tool_input(data: dict) -> "Intent":
    """Reconstruct an Intent from an agent-loop tool_use input dict (INF-02).

    Pure + headless: lives here so both the agent loop and tests build Intents the
    same way. Unknown keys are dropped; verb defaults to 'agent_task' so a
    malformed tool call still routes somewhere instead of raising.
    """
    args = dict(data.get("args") or {})
    verb = str(data.get("verb") or "agent_task")
    target = str(data.get("target") or "")
    pointer = args.pop("pointer", None)
    pointer2 = args.pop("pointer2", None)
    return Intent(
        verb=verb,
        target=target,
        args=args,
        needs_pointer=bool(pointer is not None),
        pointer=tuple(pointer) if pointer else None,
        pointer2=tuple(pointer2) if pointer2 else None,
        raw_utterance=str(data.get("raw_utterance") or target or verb),
    )


__all__ = ["Connector", "intent_from_tool_input"]

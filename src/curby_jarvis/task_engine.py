"""TaskRunner — sequential multi-step execution with per-step confirm + cancellation.

WHY: agent_loop drives its own tool-use round-trips (Anthropic SDK), but a
TaskRunner is for deterministic pre-planned step lists where the app layer
already has the Intent objects and wants human confirmation per-step before
committing irreversible actions. It also wraps the agent_loop for agentic tasks,
forwarding ProgressEvents to its own on_progress sink so the overlay stays live.

Headless: no Qt, no AppKit, no network at import time. dispatch + confirm +
on_progress + cancel_token + session are ALL injected via __init__ so tests
run with fakes and never need a real key/db/overlay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .intent import ConnectorResult, Intent, ProgressEvent


@dataclass
class Step:
    """A single intent in a planned task list with execution metadata."""
    index: int
    label: str
    intent: Intent
    risk: str = ""
    ok: Optional[bool] = None      # None = not yet run
    detail: str = ""


class TaskRunner:
    """Drive a list of Steps (or an agentic loop) sequentially.

    Args:
        dispatch:      Callable[[Intent], ConnectorResult] — e.g. router.run.
                       The caller may also pass a two-arg callable that accepts
                       an optional confirm function as its second argument; if the
                       callable accepts only one argument, confirm is not forwarded.
        confirm:       Optional callable for per-step human confirmation on
                       irreversible steps.  Signature: (step: Step) -> bool.
                       None → auto-confirm everything (use for scripted flows).
        on_progress:   Optional Callable[[ProgressEvent], None] forwarded for
                       each step + relayed from streaming agent events.
        cancel_token:  Optional object with a .cancelled() -> bool method.
                       When cancelled() returns True mid-run, execution stops and
                       returns a ConnectorResult(ok=False, error='cancelled').
        session:       Optional SessionState for recording each step. None → no
                       persistence (fine for tests).
    """

    def __init__(
        self,
        *,
        dispatch: Callable,
        confirm: Optional[Callable] = None,
        on_progress: Optional[Callable[[ProgressEvent], None]] = None,
        cancel_token=None,
        session=None,
    ):
        self._dispatch = dispatch
        self._confirm = confirm
        self._on_progress = on_progress
        self._cancel = cancel_token
        self._session = session

    # -- helpers --------------------------------------------------------------

    def _cancelled(self) -> bool:
        try:
            return bool(self._cancel and self._cancel.cancelled())
        except Exception:
            return False

    def _emit(self, event: ProgressEvent) -> None:
        """Forward a ProgressEvent to on_progress; swallow all errors."""
        if self._on_progress is None:
            return
        try:
            self._on_progress(event)
        except Exception:
            pass

    def _relay(self, event: ProgressEvent) -> None:
        """Relay callback used by run_agentic to forward agent streaming events."""
        self._emit(event)

    def _run_intent(self, intent: Intent) -> ConnectorResult:
        """Dispatch an intent, forwarding confirm when dispatch accepts it."""
        try:
            import inspect
            sig = inspect.signature(self._dispatch)
            params = list(sig.parameters.values())
            if len(params) >= 2:
                return self._dispatch(intent, self._confirm)
            return self._dispatch(intent)
        except Exception as exc:
            return ConnectorResult(ok=False, error="dispatch_exception", detail=str(exc))

    def _record(self, step: Step, res: ConnectorResult) -> None:
        """Persist the step outcome to session if available."""
        if self._session is None:
            return
        try:
            self._session.record_action(
                verb=step.intent.verb,
                target=step.intent.target,
                mechanism=res.mechanism,
                ok=res.ok,
                risk=step.risk or step.intent.risk,
                undo_label=step.label if res.undo_fn else None,  # type: ignore[attr-defined]
            )
            if res.ok and getattr(res, "undo_fn", None) is not None:
                self._session.push_undo(step.label, res.undo_fn)
        except Exception:
            pass

    # -- public API -----------------------------------------------------------

    def run_steps(self, steps: list[Intent]) -> ConnectorResult:
        """Drive a list of Intents sequentially.

        Per-step: emit a progress event, optionally confirm irreversible steps,
        check for cancellation, dispatch, record in session. Aggregates into a
        single ConnectorResult: ok=True iff all steps succeeded; steps=n_run.
        """
        if not steps:
            return ConnectorResult(ok=True, mechanism="task_engine", steps=0)

        n = len(steps)
        last = ConnectorResult(ok=False, error="no_steps_run")

        for i, intent in enumerate(steps):
            # Cancellation check before each step
            if self._cancelled():
                return ConnectorResult(
                    ok=False,
                    mechanism="task_engine",
                    error="cancelled",
                    steps=i,
                )

            label = intent.raw_utterance or intent.target or intent.verb
            step = Step(index=i, label=label, intent=intent, risk=intent.risk)

            # Emit per-step progress
            self._emit(ProgressEvent(
                phase="acting",
                text=label,
                pct=i / n,
                mechanism="task_engine",
                kind="step",
            ))

            # Per-step confirm for irreversible steps
            if intent.must_confirm and self._confirm is not None:
                try:
                    approved = self._confirm(step)
                except Exception:
                    approved = False
                if not approved:
                    step.ok = False
                    step.detail = "confirm_denied"
                    self._record(step, ConnectorResult(
                        ok=False, mechanism="task_engine", error="confirm_denied"
                    ))
                    return ConnectorResult(
                        ok=False,
                        mechanism="task_engine",
                        error="confirm_denied",
                        steps=i,
                    )

            # Dispatch the step
            res = self._run_intent(intent)
            step.ok = res.ok
            step.detail = res.detail or res.error
            last = res

            self._record(step, res)

            if not res.ok:
                # Stop on first failure
                return ConnectorResult(
                    ok=False,
                    mechanism=res.mechanism or "task_engine",
                    error=res.error,
                    detail=res.detail,
                    detail_text=res.detail_text,
                    steps=i + 1,
                )

        return ConnectorResult(
            ok=True,
            mechanism="task_engine",
            detail_text=last.detail_text,
            steps=n,
        )

    def run_agentic(self, intent: Intent, agent_loop) -> ConnectorResult:
        """Delegate to agent_loop.execute_streaming, relaying events + honouring cancel.

        agent_loop: any object with execute_streaming(intent, on_event) -> ConnectorResult.
        This runner wraps on_event so it can (a) forward to on_progress and
        (b) check cancel_token between events.
        """
        cancelled_at: list[int] = []  # mutable sentinel for closure

        def _on_event(event: ProgressEvent) -> None:
            # Check cancellation before forwarding; raise to break the streaming loop.
            if self._cancelled():
                cancelled_at.append(1)
                raise _CancelSignal()
            self._relay(event)

        try:
            res = agent_loop.execute_streaming(intent, _on_event)
        except _CancelSignal:
            return ConnectorResult(
                ok=False,
                mechanism="task_engine",
                error="cancelled",
                steps=0,
            )
        except Exception as exc:
            return ConnectorResult(
                ok=False,
                mechanism="task_engine",
                error="exception",
                detail=str(exc),
            )

        # The agent loop wraps every on_event call in `except Exception: pass`
        # (its contract), so a _CancelSignal raised mid-stream is swallowed there
        # and never reaches the handler above. Honour the sentinel post-return so
        # barge-in actually cancels instead of returning ok=True.
        if cancelled_at:
            return ConnectorResult(ok=False, mechanism="task_engine", error="cancelled",
                                   steps=getattr(res, "steps", 0))

        # Record the agentic run as a single session action
        if self._session is not None:
            try:
                self._session.record_action(
                    verb=intent.verb,
                    target=intent.target,
                    mechanism=res.mechanism or "agent_loop",
                    ok=res.ok,
                    risk=intent.risk,
                )
            except Exception:
                pass

        return res


class _CancelSignal(Exception):
    """Internal sentinel raised to abort the agent_loop streaming callback loop."""

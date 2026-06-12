"""Headless tests for TaskRunner — no display, no real network, fakes only.

Tests exercise:
- run_steps sequential execution and progress events
- run_steps cancellation mid-run
- run_steps confirm gate for irreversible steps
- run_steps first-failure stops chain
- run_agentic delegates to agent_loop.execute_streaming + relays events
- run_agentic cancellation via cancel_token
- session recording called per step
"""
from __future__ import annotations

import importlib
from typing import Optional

import pytest

from curby_jarvis.intent import ConnectorResult, Intent, ProgressEvent
from curby_jarvis.task_engine import Step, TaskRunner
from curby_jarvis.session_state import SessionState


# ---------------------------------------------------------------------------
# helpers / fakes
# ---------------------------------------------------------------------------

def ok_result(mechanism="fake") -> ConnectorResult:
    return ConnectorResult(ok=True, mechanism=mechanism)


def fail_result(error="fake_err") -> ConnectorResult:
    return ConnectorResult(ok=False, error=error)


class FakeCancelToken:
    def __init__(self, flip_after: int = 999):
        self._count = 0
        self._flip_after = flip_after

    def cancelled(self) -> bool:
        self._count += 1
        return self._count > self._flip_after


def make_dispatch(results: list[ConnectorResult]):
    """Returns a dispatch fn that pops results in order."""
    queue = list(results)
    calls: list[Intent] = []

    def dispatch(intent: Intent, confirm=None) -> ConnectorResult:
        calls.append(intent)
        return queue.pop(0) if queue else ConnectorResult(ok=False, error="no_more_results")

    dispatch.calls = calls  # type: ignore[attr-defined]
    return dispatch


def reversible(verb="play") -> Intent:
    return Intent(verb=verb, target="spotify", raw_utterance=f"{verb} spotify")


def irreversible(verb="close") -> Intent:
    # close is in IRREVERSIBLE_VERBS so must_confirm=True
    return Intent(verb=verb, target="window", raw_utterance=f"{verb} window", reversible=False)


# ---------------------------------------------------------------------------
# headless import
# ---------------------------------------------------------------------------

def test_module_imports_headless():
    m = importlib.import_module("curby_jarvis.task_engine")
    assert hasattr(m, "TaskRunner")
    assert hasattr(m, "Step")


# ---------------------------------------------------------------------------
# Step dataclass
# ---------------------------------------------------------------------------

def test_step_defaults():
    i = Intent("play", "track")
    s = Step(index=0, label="play track", intent=i)
    assert s.ok is None
    assert s.detail == ""
    assert s.risk == ""


# ---------------------------------------------------------------------------
# run_steps: basic sequential execution
# ---------------------------------------------------------------------------

def test_run_steps_empty_returns_ok():
    runner = TaskRunner(dispatch=lambda i: ok_result())
    res = runner.run_steps([])
    assert res.ok is True
    assert res.steps == 0


def test_run_steps_single_ok():
    dispatch = make_dispatch([ok_result()])
    runner = TaskRunner(dispatch=dispatch)
    res = runner.run_steps([reversible()])
    assert res.ok is True
    assert res.steps == 1
    assert len(dispatch.calls) == 1


def test_run_steps_multiple_all_ok():
    intents = [reversible("play"), reversible("next"), reversible("pause")]
    dispatch = make_dispatch([ok_result(), ok_result(), ok_result()])
    runner = TaskRunner(dispatch=dispatch)
    res = runner.run_steps(intents)
    assert res.ok is True
    assert res.steps == 3
    assert len(dispatch.calls) == 3


def test_run_steps_stops_on_first_failure():
    intents = [reversible("play"), reversible("next"), reversible("pause")]
    dispatch = make_dispatch([ok_result(), fail_result("step2_err"), ok_result()])
    runner = TaskRunner(dispatch=dispatch)
    res = runner.run_steps(intents)
    assert res.ok is False
    assert res.error == "step2_err"
    assert res.steps == 2  # ran step 0 and step 1, stopped
    assert len(dispatch.calls) == 2  # step 2 never dispatched


# ---------------------------------------------------------------------------
# run_steps: per-step progress events
# ---------------------------------------------------------------------------

def test_run_steps_emits_progress_events():
    events: list[ProgressEvent] = []
    dispatch = make_dispatch([ok_result(), ok_result()])
    runner = TaskRunner(dispatch=dispatch, on_progress=events.append)
    runner.run_steps([reversible(), reversible()])
    assert len(events) == 2
    assert all(e.kind == "step" for e in events)
    assert all(e.phase == "acting" for e in events)
    assert events[0].pct == 0.0
    assert events[1].pct == 0.5


def test_run_steps_no_progress_when_none():
    """on_progress=None must not raise."""
    dispatch = make_dispatch([ok_result()])
    runner = TaskRunner(dispatch=dispatch, on_progress=None)
    res = runner.run_steps([reversible()])
    assert res.ok is True


# ---------------------------------------------------------------------------
# run_steps: cancellation
# ---------------------------------------------------------------------------

def test_run_steps_cancelled_before_first_step():
    cancel = FakeCancelToken(flip_after=0)  # immediately cancelled
    dispatch = make_dispatch([ok_result(), ok_result(), ok_result()])
    runner = TaskRunner(dispatch=dispatch, cancel_token=cancel)
    res = runner.run_steps([reversible(), reversible(), reversible()])
    assert res.ok is False
    assert res.error == "cancelled"
    assert len(dispatch.calls) == 0  # never dispatched


def test_run_steps_cancelled_mid_run():
    """Cancel after step 1: step 0 runs, step 1 is cancelled before dispatch."""
    cancel = FakeCancelToken(flip_after=1)  # cancelled() returns True on call >1
    dispatched: list[Intent] = []

    def dispatch(intent, confirm=None):
        dispatched.append(intent)
        return ok_result()

    runner = TaskRunner(dispatch=dispatch, cancel_token=cancel)
    intents = [reversible("play"), reversible("next"), reversible("pause")]
    res = runner.run_steps(intents)
    assert res.ok is False
    assert res.error == "cancelled"
    # Exactly one step dispatched before the cancel fires
    assert len(dispatched) == 1


# ---------------------------------------------------------------------------
# run_steps: confirm gate
# ---------------------------------------------------------------------------

def test_run_steps_confirm_approved():
    approved: list[Step] = []
    dispatch = make_dispatch([ok_result()])

    def confirm(step: Step) -> bool:
        approved.append(step)
        return True

    runner = TaskRunner(dispatch=dispatch, confirm=confirm)
    res = runner.run_steps([irreversible()])
    assert res.ok is True
    assert len(approved) == 1
    assert approved[0].label == "close window"


def test_run_steps_confirm_denied_stops_early():
    dispatch = make_dispatch([ok_result(), ok_result()])

    def confirm(step: Step) -> bool:
        return False  # always deny

    runner = TaskRunner(dispatch=dispatch, confirm=confirm)
    res = runner.run_steps([irreversible(), reversible()])
    assert res.ok is False
    assert res.error == "confirm_denied"
    assert len(dispatch.calls) == 0  # never executed


def test_run_steps_no_confirm_autorun_reversible():
    """Reversible intents with no confirm fn should run without asking."""
    dispatch = make_dispatch([ok_result()])
    runner = TaskRunner(dispatch=dispatch, confirm=None)
    res = runner.run_steps([reversible()])
    assert res.ok is True


# ---------------------------------------------------------------------------
# run_steps: session recording
# ---------------------------------------------------------------------------

def test_run_steps_records_to_session():
    session = SessionState(db_path=":memory:")
    dispatch = make_dispatch([ok_result("ax_press"), ok_result("ax_press")])
    runner = TaskRunner(dispatch=dispatch, session=session)
    runner.run_steps([reversible("play"), reversible("next")])
    rows = session.recent()
    assert len(rows) == 2
    verbs = {r["verb"] for r in rows}
    assert "play" in verbs
    assert "next" in verbs


def test_run_steps_no_session_never_raises():
    dispatch = make_dispatch([ok_result()])
    runner = TaskRunner(dispatch=dispatch, session=None)
    res = runner.run_steps([reversible()])
    assert res.ok is True


# ---------------------------------------------------------------------------
# run_agentic: delegates to agent_loop.execute_streaming
# ---------------------------------------------------------------------------

class FakeAgentLoop:
    """Scripted fake that emits a fixed sequence of events then returns a result."""

    def __init__(self, events: list[ProgressEvent], result: ConnectorResult):
        self._events = events
        self._result = result
        self.calls: list[Intent] = []

    def execute_streaming(self, intent: Intent, on_event) -> ConnectorResult:
        self.calls.append(intent)
        for ev in self._events:
            on_event(ev)
        return self._result


def test_run_agentic_relays_events():
    relayed: list[ProgressEvent] = []
    events = [
        ProgressEvent(phase="acting", text="tool call 1", kind="tool_call"),
        ProgressEvent(phase="acting", text="tool result 1", kind="tool_result"),
    ]
    loop = FakeAgentLoop(events, ok_result("agent_loop"))
    runner = TaskRunner(dispatch=lambda i: ok_result(), on_progress=relayed.append)
    res = runner.run_agentic(Intent("agent_task", raw_utterance="do it"), loop)
    assert res.ok is True
    assert len(relayed) == 2
    assert relayed[0].kind == "tool_call"
    assert relayed[1].kind == "tool_result"


def test_run_agentic_returns_loop_result():
    loop = FakeAgentLoop([], ConnectorResult(ok=True, mechanism="agent_loop", steps=3))
    runner = TaskRunner(dispatch=lambda i: ok_result())
    res = runner.run_agentic(Intent("agent_task"), loop)
    assert res.ok is True
    assert res.steps == 3


def test_run_agentic_cancel_token_stops_mid_stream():
    """When cancel_token flips mid-event-stream, run_agentic returns cancelled."""
    cancel = FakeCancelToken(flip_after=1)
    relayed: list[ProgressEvent] = []
    events = [
        ProgressEvent(phase="acting", text="step 1", kind="tool_call"),
        ProgressEvent(phase="acting", text="step 2", kind="tool_call"),
        ProgressEvent(phase="acting", text="step 3", kind="tool_call"),
    ]
    loop = FakeAgentLoop(events, ok_result("agent_loop"))
    runner = TaskRunner(
        dispatch=lambda i: ok_result(),
        on_progress=relayed.append,
        cancel_token=cancel,
    )
    res = runner.run_agentic(Intent("agent_task"), loop)
    assert res.ok is False
    assert res.error == "cancelled"
    # Some events may have been relayed before cancellation, but not all 3
    assert len(relayed) < 3


def test_run_agentic_records_to_session():
    session = SessionState(db_path=":memory:")
    loop = FakeAgentLoop([], ok_result("agent_loop"))
    runner = TaskRunner(dispatch=lambda i: ok_result(), session=session)
    runner.run_agentic(Intent("agent_task", target="organize files"), loop)
    rows = session.recent(1)
    assert len(rows) == 1
    assert rows[0]["verb"] == "agent_task"
    assert rows[0]["target"] == "organize files"


def test_run_agentic_failed_loop_records_ok_false():
    session = SessionState(db_path=":memory:")
    loop = FakeAgentLoop([], ConnectorResult(ok=False, error="agent_err", mechanism="agent_loop"))
    runner = TaskRunner(dispatch=lambda i: ok_result(), session=session)
    res = runner.run_agentic(Intent("agent_task"), loop)
    assert res.ok is False
    rows = session.recent(1)
    assert rows[0]["ok"] == 0


def test_run_agentic_never_raises_on_loop_exception():
    class BoomLoop:
        def execute_streaming(self, intent, on_event):
            raise RuntimeError("loop exploded")

    runner = TaskRunner(dispatch=lambda i: ok_result())
    res = runner.run_agentic(Intent("agent_task"), BoomLoop())
    assert res.ok is False
    assert res.error == "exception"
    assert "loop exploded" in res.detail

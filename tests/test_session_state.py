"""Headless tests for SessionState — no real display, no real db at $HOME.

All tests use db_path=':memory:' so no filesystem I/O occurs under $HOME.
"""
from __future__ import annotations

import importlib
import pytest

from curby_jarvis.session_state import SessionState


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def fresh() -> SessionState:
    return SessionState(db_path=":memory:")


# ---------------------------------------------------------------------------
# headless import contract
# ---------------------------------------------------------------------------

def test_module_imports_headless():
    m = importlib.import_module("curby_jarvis.session_state")
    assert hasattr(m, "SessionState")


# ---------------------------------------------------------------------------
# record_action + recent + last_verb + last_target
# ---------------------------------------------------------------------------

def test_record_action_returns_positive_rowid():
    s = fresh()
    rid = s.record_action("open", "spotify", "ax_press", True, risk="launch")
    assert rid > 0


def test_last_verb_and_target_after_record():
    s = fresh()
    s.record_action("play", "track1", "media_key", True)
    s.record_action("pause", "track1", "media_key", True)
    assert s.last_verb() == "pause"
    assert s.last_target() == "track1"


def test_last_verb_none_on_empty_db():
    s = fresh()
    assert s.last_verb() is None
    assert s.last_target() is None


def test_recent_returns_n_newest_first():
    s = fresh()
    for i in range(5):
        s.record_action(f"verb{i}", f"t{i}", "m", True)
    rows = s.recent(3)
    assert len(rows) == 3
    # newest first
    assert rows[0]["verb"] == "verb4"
    assert rows[1]["verb"] == "verb3"


def test_recent_empty_db_returns_empty_list():
    s = fresh()
    assert s.recent() == []


def test_record_action_ok_false():
    s = fresh()
    rid = s.record_action("close", "window", "ax_press", False, risk="irreversible")
    assert rid > 0
    rows = s.recent(1)
    assert rows[0]["ok"] == 0


# ---------------------------------------------------------------------------
# undo ring
# ---------------------------------------------------------------------------

def test_push_pop_undo():
    s = fresh()
    called = []
    fn = lambda: called.append(1) or True
    uid = s.push_undo("close spotify", fn)
    assert isinstance(uid, str) and len(uid) == 32  # uuid4 hex

    result = s.pop_undo()
    assert result is not None
    label, undo_fn = result
    assert label == "close spotify"
    undo_fn()
    assert called == [1]


def test_pop_undo_empty_returns_none():
    s = fresh()
    assert s.pop_undo() is None


def test_peek_undo_does_not_remove():
    s = fresh()
    s.push_undo("undo me", lambda: True)
    peeked = s.peek_undo()
    assert peeked is not None
    assert peeked[0] == "undo me"
    # still there after peek
    assert s.pop_undo() is not None


def test_peek_undo_empty_returns_none():
    s = fresh()
    assert s.peek_undo() is None


def test_undo_ring_max_size_32():
    """Ring should not grow beyond 32 entries (deque maxlen)."""
    s = fresh()
    for i in range(40):
        s.push_undo(f"step {i}", lambda: True)
    # After 40 pushes, maxlen=32 means oldest 8 are dropped
    # The ring should have exactly 32 entries: pop all of them
    count = 0
    while s.pop_undo() is not None:
        count += 1
    assert count == 32


def test_undo_ring_multiple_unique_ids():
    s = fresh()
    uid1 = s.push_undo("a", lambda: True)
    uid2 = s.push_undo("b", lambda: True)
    assert uid1 != uid2


# ---------------------------------------------------------------------------
# pending task slot
# ---------------------------------------------------------------------------

def test_set_get_clear_pending_task():
    s = fresh()
    assert s.get_pending_task() is None
    task = {"steps": ["do x", "do y"], "label": "complex task"}
    s.set_pending_task(task)
    got = s.get_pending_task()
    assert got == task
    # ensure it's a copy, not the original dict reference
    task["label"] = "mutated"
    assert s.get_pending_task()["label"] == "complex task"


def test_clear_pending_task():
    s = fresh()
    s.set_pending_task({"x": 1})
    s.clear_pending_task()
    assert s.get_pending_task() is None


def test_set_pending_task_replaces_previous():
    s = fresh()
    s.set_pending_task({"first": True})
    s.set_pending_task({"second": True})
    assert s.get_pending_task() == {"second": True}


# ---------------------------------------------------------------------------
# error resilience
# ---------------------------------------------------------------------------

def test_record_action_never_raises_on_bad_db(monkeypatch):
    """If sqlite is broken, record_action returns 0 without raising."""
    s = fresh()
    s._ensure_conn()
    # Corrupt the connection by closing it
    s._conn.close()
    rid = s.record_action("verb", "target", "mech", True)
    assert rid == 0  # graceful degradation


def test_recent_never_raises_on_bad_db(monkeypatch):
    s = fresh()
    s._ensure_conn()
    s._conn.close()
    rows = s.recent()
    assert rows == []

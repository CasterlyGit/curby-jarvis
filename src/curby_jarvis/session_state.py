"""SessionState — cross-session memory for curby-jarvis actions and undo.

WHY: the assistant needs to answer "undo that" and "what did I just do?" without
keeping everything in-process. SQLite gives durable action history; an in-memory
undo ring (plain callables) handles live undo without serialising functions.

Headless: nothing here touches Qt, AppKit, Quartz, or the network at import time.
All sqlite3 access is lazy (first call). db_path is injected so tests can pass
':memory:' and never write to $HOME.
"""
from __future__ import annotations

import json
import time
import uuid
from collections import deque
from typing import Callable, Optional


class SessionState:
    """Durable action log (SQLite) + in-memory undo ring.

    db_path=None  -> ~/.curby/session.db (created on first use).
    db_path=':memory:' -> pure in-memory (great for tests; no disk I/O).
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS actions (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        REAL    NOT NULL,
        verb      TEXT    NOT NULL,
        target    TEXT    NOT NULL DEFAULT '',
        mechanism TEXT    NOT NULL DEFAULT '',
        ok        INTEGER NOT NULL DEFAULT 0,
        risk      TEXT    NOT NULL DEFAULT '',
        undo_label TEXT
    );
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path  # resolved lazily on first connect
        self._conn = None
        # undo ring: deque of (undo_id, label, callable); max 32 entries
        self._undo: deque[tuple[str, str, Callable[[], bool]]] = deque(maxlen=32)
        # pending task slot (dict or None)
        self._pending_task: Optional[dict] = None

    # -- connection management -------------------------------------------------

    def _ensure_conn(self):
        """Open and initialise the SQLite connection on first call. Never raises."""
        if self._conn is not None:
            return
        try:
            import sqlite3
            import os
            from pathlib import Path

            if self._db_path is None:
                p = Path(os.path.expanduser("~/.curby/session.db"))
                p.parent.mkdir(parents=True, exist_ok=True)
                path = str(p)
            else:
                path = self._db_path

            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(self._SCHEMA)
            self._conn.commit()
        except Exception:
            self._conn = None

    def close(self) -> None:
        """Explicitly close the SQLite connection. Idempotent + never raises —
        called from app.run_live's finally so no -wal file lingers after exit."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # -- action log -----------------------------------------------------------

    def record_action(
        self,
        verb: str,
        target: str,
        mechanism: str,
        ok: bool,
        risk: str = "",
        undo_label: Optional[str] = None,
    ) -> int:
        """Append one action row; return the new rowid (or 0 on failure)."""
        self._ensure_conn()
        try:
            cur = self._conn.execute(
                "INSERT INTO actions (ts, verb, target, mechanism, ok, risk, undo_label) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), verb, target, mechanism, int(ok), risk, undo_label),
            )
            self._conn.commit()
            return cur.lastrowid or 0
        except Exception:
            return 0

    def last_target(self) -> Optional[str]:
        """Return the target of the most recent action, or None."""
        self._ensure_conn()
        try:
            row = self._conn.execute(
                "SELECT target FROM actions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row["target"] if row else None
        except Exception:
            return None

    def last_verb(self) -> Optional[str]:
        """Return the verb of the most recent action, or None."""
        self._ensure_conn()
        try:
            row = self._conn.execute(
                "SELECT verb FROM actions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row["verb"] if row else None
        except Exception:
            return None

    def recent(self, n: int = 20) -> list[dict]:
        """Return the n most recent actions as plain dicts, newest first."""
        self._ensure_conn()
        try:
            rows = self._conn.execute(
                "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    # -- undo ring (in-memory, callables never stored in SQLite) --------------

    def push_undo(self, label: str, undo_fn: Callable[[], bool]) -> str:
        """Push an undo entry. Returns a unique undo_id string."""
        undo_id = uuid.uuid4().hex
        self._undo.append((undo_id, label, undo_fn))
        return undo_id

    def pop_undo(self) -> Optional[tuple[str, Callable[[], bool]]]:
        """Pop the most recent undo entry as (label, undo_fn), or None if empty."""
        if not self._undo:
            return None
        _uid, label, fn = self._undo.pop()
        return (label, fn)

    def peek_undo(self) -> Optional[tuple[str, Callable[[], bool]]]:
        """Inspect the most recent undo entry without removing it, or None."""
        if not self._undo:
            return None
        _uid, label, fn = self._undo[-1]
        return (label, fn)

    # -- pending task slot ---------------------------------------------------

    def set_pending_task(self, task: dict) -> None:
        """Store a pending multi-step task description (replaces previous)."""
        self._pending_task = dict(task)

    def get_pending_task(self) -> Optional[dict]:
        """Return the current pending task dict, or None."""
        return self._pending_task

    def clear_pending_task(self) -> None:
        """Clear the pending task slot."""
        self._pending_task = None

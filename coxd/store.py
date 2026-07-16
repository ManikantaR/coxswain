"""Single-owner task state + event log (SQLite) — DESIGN-V35.

The old system had THREE uncoordinated state owners (CLI, watcher, LLM-reading-
prose), so meta.json could say "working" after a task had gated, reviewed, and
failed. Here `coxd` is the ONLY writer: every phase transition and event goes
through this module into one SQLite db. The board is a stateless reader over it;
if the board (or coxd) restarts, the db is the truth and sessions resume.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import registry


def db_path() -> Path:
    return registry.home() / "coxd.sqlite"


def _conn() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks(
            id TEXT PRIMARY KEY, repo TEXT, repo_path TEXT, brief TEXT, state TEXT,
            reason TEXT, session_id TEXT, worktree TEXT, cost REAL DEFAULT 0,
            created REAL, updated REAL);
        CREATE TABLE IF NOT EXISTS events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, ts REAL,
            kind TEXT, data TEXT);
        """
    )
    return c


def create_task(task_id: str, repo: str, brief: str, worktree: str,
                repo_path: str = "") -> None:
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tasks"
            "(id,repo,repo_path,brief,state,worktree,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task_id, repo, repo_path, brief, "queued", worktree, now, now),
        )
    append_event(task_id, "created", {"repo": repo})


def set_state(task_id: str, state: str, reason: str | None = None) -> None:
    with _conn() as c:
        c.execute("UPDATE tasks SET state=?, reason=?, updated=? WHERE id=?",
                  (state, reason, time.time(), task_id))
    append_event(task_id, "state", {"state": state, "reason": reason})


def set_session(task_id: str, session_id: str | None) -> None:
    with _conn() as c:
        c.execute("UPDATE tasks SET session_id=?, updated=? WHERE id=?",
                  (session_id, time.time(), task_id))


def add_cost(task_id: str, delta: float | None) -> None:
    if not delta:
        return
    with _conn() as c:
        c.execute("UPDATE tasks SET cost=cost+?, updated=? WHERE id=?",
                  (delta, time.time(), task_id))


def append_event(task_id: str, kind: str, data: dict | None = None) -> None:
    with _conn() as c:
        c.execute("INSERT INTO events(task_id,ts,kind,data) VALUES(?,?,?,?)",
                  (task_id, time.time(), kind, json.dumps(data or {})))


def get_task(task_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(r) if r else None


def list_tasks() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM tasks ORDER BY created DESC")]


def queued_tasks() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in
                c.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY created")]


def events(task_id: str, after_seq: int = 0) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT seq,ts,kind,data FROM events WHERE task_id=? AND seq>? ORDER BY seq",
            (task_id, after_seq),
        ).fetchall()
    return [{"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "data": json.loads(r["data"])}
            for r in rows]

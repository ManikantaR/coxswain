"""Single-owner task state + event log (SQLite) — DESIGN-V35.

The old system had THREE uncoordinated state owners (CLI, watcher, LLM-reading-
prose), so meta.json could say "working" after a task had gated, reviewed, and
failed. Here `coxd` is the ONLY writer: every phase transition and event goes
through this module into one SQLite db. The board is a stateless reader over it;
if the board (or coxd) restarts, the db is the truth and sessions resume.
"""

from __future__ import annotations

import json
import os
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
            pr_url TEXT, created REAL, updated REAL);
        CREATE TABLE IF NOT EXISTS events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, ts REAL,
            kind TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS rule_suggestions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, text TEXT,
            source TEXT, evidence TEXT, status TEXT DEFAULT 'pending',
            created REAL, decided REAL);
        CREATE TABLE IF NOT EXISTS repo_streaks(
            repo TEXT PRIMARY KEY, clean_streak INTEGER NOT NULL DEFAULT 0, updated REAL);
        CREATE TABLE IF NOT EXISTS cost_ledger(
            task_id TEXT, ts REAL, delta REAL);
        CREATE INDEX IF NOT EXISTS cost_ts ON cost_ledger(ts);
        CREATE INDEX IF NOT EXISTS ev_task ON events(task_id, seq);
        """
    )
    # migrate older dbs: CREATE TABLE IF NOT EXISTS won't add columns to an
    # existing table, so add any that are missing (self-healing schema).
    have = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    for col, ddl in (("repo_path", "TEXT"), ("pr_url", "TEXT"), ("cost", "REAL DEFAULT 0"),
                     ("summary", "TEXT"), ("gate_steps", "TEXT"),
                     ("review_verdict", "TEXT"), ("review_findings", "TEXT"),
                     ("test_output", "TEXT"), ("archived_at", "REAL"),
                     ("intent", "TEXT"), ("resume_at", "REAL"),
                     ("greenlight_tier", "TEXT"), ("greenlight_reasons", "TEXT")):
        if col not in have:
            c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
    return c


def create_task(task_id: str, repo: str, brief: str, worktree: str,
                repo_path: str = "", intent: str = "") -> None:
    """`brief` is the full implementer prompt (rules block + intent); `intent` is
    the captain's raw one-liner, kept separate so ship.py titles the PR from the
    real intent, not the prepended rules heading."""
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO tasks"
            "(id,repo,repo_path,brief,intent,state,worktree,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, repo, repo_path, brief, intent or brief, "queued", worktree, now, now),
        )
    append_event(task_id, "created", {"repo": repo})


def set_state(task_id: str, state: str, reason: str | None = None) -> None:
    with _conn() as c:
        c.execute("UPDATE tasks SET state=?, reason=?, updated=? WHERE id=?",
                  (state, reason, time.time(), task_id))
    append_event(task_id, "state", {"state": state, "reason": reason})


def cas_state(task_id: str, expect: str, state: str, reason: str | None = None,
              expect_reason: str | None = ...) -> bool:
    """Compare-and-set: change state only if it is still `expect` (and, when given,
    the reason is still `expect_reason`). Returns whether it applied."""
    sql = "UPDATE tasks SET state=?, reason=?, updated=? WHERE id=? AND state=?"
    args = [state, reason, time.time(), task_id, expect]
    if expect_reason is not ...:
        sql += " AND reason IS ?"
        args.append(expect_reason)
    with _conn() as c:
        ok = c.execute(sql, args).rowcount == 1
    if ok:
        append_event(task_id, "state", {"state": state, "reason": reason})
    return ok


def set_pr_url(task_id: str, url: str) -> None:
    with _conn() as c:
        c.execute("UPDATE tasks SET pr_url=?, updated=? WHERE id=?",
                  (url, time.time(), task_id))


def set_greenlight_tier(task_id: str, tier: str | None,
                        reasons: list[str] | None = None) -> None:
    """Store the tier and its reasons (JSON) so landed.is_clean_merge can tell a
    size-only AMBER apart from one with medium findings."""
    with _conn() as c:
        c.execute("UPDATE tasks SET greenlight_tier=?, greenlight_reasons=?, updated=? WHERE id=?",
                  (tier, json.dumps(reasons or []), time.time(), task_id))


def get_clean_streak(repo: str) -> int:
    with _conn() as c:
        r = c.execute("SELECT clean_streak FROM repo_streaks WHERE repo=?", (repo,)).fetchone()
    return int(r[0]) if r else 0


def propose_auto_merge(repo: str) -> bool:
    """If the repo's clean streak reached the threshold, move manual → proposed.
    True only on that transition (one notification, never repeated)."""
    if get_clean_streak(repo) < registry.AUTO_MERGE_STREAK:
        return False
    return registry.propose_auto_merge(repo)


def approve_auto_merge(repo: str) -> bool:
    """proposed → auto, only while the streak still justifies the proposal."""
    if get_clean_streak(repo) < registry.AUTO_MERGE_STREAK:
        registry.withdraw_proposal(repo)
        return False
    return registry.approve_auto_merge(repo)


def record_merge(repo: str, clean: bool, task_id: str | None = None) -> int:
    """A clean merge bumps the repo's streak; any non-clean merge resets it to 0.
    With task_id it is idempotent: the dedupe check, the counter update and the
    'streak' event share one transaction, so a re-run (or a crash after the update)
    never double-counts. Returns the current streak."""
    with _conn() as c:
        if task_id is not None and c.execute(
                "SELECT 1 FROM events WHERE task_id=? AND kind='streak'",
                (task_id,)).fetchone():
            r = c.execute("SELECT clean_streak FROM repo_streaks WHERE repo=?",
                          (repo,)).fetchone()
            return int(r[0]) if r else 0
        flag = 1 if clean else 0
        c.execute(
            "INSERT INTO repo_streaks(repo,clean_streak,updated) VALUES(?,?,?) "
            "ON CONFLICT(repo) DO UPDATE SET "
            "clean_streak=CASE WHEN ?=1 THEN clean_streak+1 ELSE 0 END, updated=excluded.updated",
            (repo, flag, time.time(), flag))
        n = int(c.execute("SELECT clean_streak FROM repo_streaks WHERE repo=?",
                          (repo,)).fetchone()[0])
        if task_id is not None:
            c.execute("INSERT INTO events(task_id,ts,kind,data) VALUES(?,?,?,?)",
                      (task_id, time.time(), "streak",
                       json.dumps({"clean": clean, "streak": n})))
    if task_id is not None:
        import status
        status.refresh_for_task(task_id)
    return n


def set_resume_at(task_id: str, ts: float | None) -> None:
    """When a task parked at `waiting_quota` may continue (epoch seconds)."""
    with _conn() as c:
        c.execute("UPDATE tasks SET resume_at=?, updated=? WHERE id=?",
                  (ts, time.time(), task_id))


def waiting_quota_tasks() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM tasks WHERE state='waiting_quota' ORDER BY created")]


def set_session(task_id: str, session_id: str | None) -> None:
    with _conn() as c:
        c.execute("UPDATE tasks SET session_id=?, updated=? WHERE id=?",
                  (session_id, time.time(), task_id))


def add_cost(task_id: str, delta: float | None) -> None:
    if not delta:
        return
    with _conn() as c:
        now = time.time()
        c.execute("UPDATE tasks SET cost=cost+?, updated=? WHERE id=?",
                  (delta, now, task_id))
        c.execute("INSERT INTO cost_ledger(task_id,ts,delta) VALUES(?,?,?)",
                  (task_id, now, delta))


def set_summary(task_id: str, summary: str | None) -> None:
    """The worker's own final wrap-up text — feeds the PR body (see ship.py)."""
    if not summary:
        return
    with _conn() as c:
        c.execute("UPDATE tasks SET summary=?, updated=? WHERE id=?",
                  (summary, time.time(), task_id))


def set_gate_steps(task_id: str, steps: dict | None, test_output: str | None = None) -> None:
    with _conn() as c:
        c.execute("UPDATE tasks SET gate_steps=?, test_output=?, updated=? WHERE id=?",
                  (json.dumps(steps or {}), test_output, time.time(), task_id))


def set_review(task_id: str, verdict: str | None, findings: list[dict] | None) -> None:
    with _conn() as c:
        c.execute("UPDATE tasks SET review_verdict=?, review_findings=?, updated=? WHERE id=?",
                  (verdict, json.dumps(findings or []), time.time(), task_id))


def archive_task(task_id: str) -> None:
    """Manually archive a task now, regardless of age — the board's per-tile Archive
    button. Distinct from the age-based auto-archive in board.py's `_is_archived`,
    which this timestamp also satisfies once set (both checks share the same field)."""
    with _conn() as c:
        c.execute("UPDATE tasks SET archived_at=?, updated=? WHERE id=?",
                  (time.time(), time.time(), task_id))
    import status  # lazy: status imports store
    status.refresh_for_task(task_id)


def unarchive_task(task_id: str) -> None:
    with _conn() as c:
        c.execute("UPDATE tasks SET archived_at=NULL, updated=? WHERE id=?",
                  (time.time(), task_id))
    import status  # lazy: status imports store
    status.refresh_for_task(task_id)


_STATUS_KINDS = {"created", "state", "deployed", "streak"}


def append_event(task_id: str, kind: str, data: dict | None = None) -> None:
    with _conn() as c:
        c.execute("INSERT INTO events(task_id,ts,kind,data) VALUES(?,?,?,?)",
                  (task_id, time.time(), kind, json.dumps(data or {})))
    if kind in _STATUS_KINDS:
        import status  # lazy: status imports store
        status.refresh_for_task(task_id)


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


def cost_today(repo: str, now: float | None = None) -> float:
    """USD spent today (local midnight onward) on a repo's tasks, summed from the
    timestamped per-spend ledger — unrelated task updates (merge, archive) don't count."""
    import datetime as _dt
    n = _dt.datetime.fromtimestamp(now if now is not None else time.time())
    midnight = n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with _conn() as c:
        r = c.execute("SELECT COALESCE(SUM(l.delta),0) FROM cost_ledger l "
                      "JOIN tasks t ON t.id=l.task_id WHERE t.repo=? AND l.ts>=?",
                      (repo, midnight)).fetchone()
    return float(r[0] or 0)


def events(task_id: str, after_seq: int = 0) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT seq,ts,kind,data FROM events WHERE task_id=? AND seq>? ORDER BY seq",
            (task_id, after_seq),
        ).fetchall()
    return [{"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "data": json.loads(r["data"])}
            for r in rows]


def add_rule_suggestion(repo: str, text: str, source: str, evidence: str = "") -> int:
    """Record a machine-discovered candidate rule (pending captain review). Never
    auto-applied — the board approves/rejects (see rules.py's add_rule)."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO rule_suggestions(repo,text,source,evidence,status,created) "
            "VALUES(?,?,?,?,'pending',?)",
            (repo, text, source, evidence, time.time()),
        )
        return cur.lastrowid


def list_rule_suggestions(status: str = "pending") -> list[dict]:
    with _conn() as c:
        if status:
            rows = c.execute(
                "SELECT * FROM rule_suggestions WHERE status=? ORDER BY created DESC",
                (status,),
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM rule_suggestions ORDER BY created DESC").fetchall()
    return [dict(r) for r in rows]


def get_rule_suggestion(sid: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM rule_suggestions WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None


def decide_rule_suggestion(sid: int, status: str) -> None:
    """status is 'approved' or 'rejected' — rejected rows are kept for audit and
    never resurfaced (status filter in list_rule_suggestions excludes them)."""
    with _conn() as c:
        c.execute("UPDATE rule_suggestions SET status=?, decided=? WHERE id=?",
                  (status, time.time(), sid))


# Terminal, resolved states eligible for silent age-based archiving (see board.py).
ARCHIVABLE = {"landed", "failed"}
ARCHIVE_DAYS = float(os.environ.get("COXD_ARCHIVE_DAYS", "14"))


def is_archived(t: dict) -> bool:
    if t.get("archived_at"):  # manually archived via the board's per-tile button
        return True
    if t["state"] not in ARCHIVABLE:
        return False
    age_days = (time.time() - (t["updated"] or t["created"] or 0)) / 86400
    return age_days > ARCHIVE_DAYS

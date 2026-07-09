"""Zero-token watcher (T-08, DESIGN §2.2).

A single Python loop over active tasks. It reads new status-log lines from a
saved offset, classifies them deterministically (classify.py), and enqueues
actionable wakes BEFORE advancing the offset (crash-safe). It also synthesizes
derived wakes — worker-exited (pid dead, no terminal line) and worker-stale
(no activity past STALE_SECS). It spends no LLM tokens and, in M0, makes no
network calls (scheduled CI checks arrive in T-11).
"""

from __future__ import annotations

import time
from pathlib import Path

from . import home, proc, store, wakequeue
from .classify import classify
from .model import TaskState

POLL_SECS = 15
STALE_SECS = 900
_ACTIVE = {TaskState.PLANNING, TaskState.WORKING, TaskState.GATING, TaskState.FIXING}
_TERMINAL_VERBS = {"done", "failed", "blocked"}


def _offset_path(task_id: str) -> Path:
    return store.task_state_dir(task_id) / "watch.offset"


def _read_offset(task_id: str) -> int:
    p = _offset_path(task_id)
    return int(p.read_text()) if p.exists() else 0


def _write_offset(task_id: str, offset: int) -> None:
    _offset_path(task_id).write_text(str(offset), encoding="utf-8")


def _pid_alive(task_id: str) -> bool | None:
    p = store.task_state_dir(task_id) / "pid"
    if not p.exists():
        return None
    try:
        return proc.is_alive(int(p.read_text().strip()))
    except ValueError:
        return None


def scan_task(task_id: str) -> int:
    """Scan one task's new status lines + liveness. Returns wakes enqueued.

    Ordering matters (DESIGN §2.2): enqueue each actionable wake, then advance
    the offset. A crash before the offset write replays the lines; wakequeue
    dedupe makes replay idempotent.
    """
    added = 0
    log = store.task_data_dir(task_id) / "status.log"
    saw_terminal = False
    if log.exists():
        text = log.read_text(encoding="utf-8")
        start = _read_offset(task_id)
        new = text[start:]
        for line in new.splitlines():
            wake = classify(line)
            if wake is None:
                continue
            if wake.verb in _TERMINAL_VERBS:
                saw_terminal = True
            if wakequeue.enqueue(task_id, wake.verb, wake.detail, line):
                added += 1
        _write_offset(task_id, len(text))

    # Derived: worker process gone without a terminal line.
    alive = _pid_alive(task_id)
    if alive is False and not saw_terminal and not _has_terminal_line(task_id):
        if wakequeue.enqueue(task_id, "worker-exited", "worker process gone", "worker-exited"):
            added += 1

    # Derived: stale (re-fires at most hourly via a coarse bucket in the key).
    if _is_stale(task_id) and not _has_terminal_line(task_id):
        bucket = int(time.time() // 3600)
        if wakequeue.enqueue(task_id, "stale", f"no activity > {STALE_SECS}s", f"stale-{bucket}"):
            added += 1
    return added


def _has_terminal_line(task_id: str) -> bool:
    log = store.task_data_dir(task_id) / "status.log"
    if not log.exists():
        return False
    for line in log.read_text(encoding="utf-8").splitlines():
        w = classify(line)
        if w and w.verb in _TERMINAL_VERBS:
            return True
    return False


def _is_stale(task_id: str) -> bool:
    log = store.task_data_dir(task_id) / "status.log"
    wlog = store.task_data_dir(task_id) / "worker.log"
    mtimes = [f.stat().st_mtime for f in (log, wlog) if f.exists()]
    if not mtimes:
        return False
    return (time.time() - max(mtimes)) > STALE_SECS


def scan_once() -> int:
    """One full pass over active tasks. Returns total wakes enqueued."""
    total = 0
    for tid in store.list_task_ids():
        if store.load_meta(tid).state in _ACTIVE:
            total += scan_task(tid)
    _write_heartbeat()
    return total


def _write_heartbeat() -> None:
    home.ensure_home()
    (home.state_dir() / "watcher.heartbeat").write_text(str(time.time()), encoding="utf-8")


def heartbeat_age() -> float | None:
    p = home.state_dir() / "watcher.heartbeat"
    if not p.exists():
        return None
    try:
        return time.time() - float(p.read_text().strip())
    except ValueError:
        return None


def run(poll_secs: int = POLL_SECS) -> None:  # pragma: no cover - long-running loop
    """The `cox watch` process. Observes only while PAUSED."""
    while True:
        if not home.is_paused():
            scan_once()
        else:
            _write_heartbeat()
        time.sleep(poll_secs)

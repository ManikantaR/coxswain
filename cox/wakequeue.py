"""Durable wake queue (T-08, DESIGN §2.2).

Actionable events are appended to state/wake-queue.jsonl BEFORE the watcher
advances its per-task scan offset, so a crash between "saw it" and "recorded
it" replays rather than loses the wake. Each entry carries a dedupe key
(task, verb, line-hash); delivery is marked in place (atomic rewrite) so the
orchestrator's `await-wake` never double-delivers.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import home


@dataclass(frozen=True)
class WakeEntry:
    task_id: str
    verb: str
    detail: str
    key: str
    ts: float
    delivered: bool = False

    def to_json(self) -> dict:
        return {
            "task_id": self.task_id,
            "verb": self.verb,
            "detail": self.detail,
            "key": self.key,
            "ts": self.ts,
            "delivered": self.delivered,
        }


def _path() -> Path:
    return home.state_dir() / "wake-queue.jsonl"


def dedupe_key(task_id: str, verb: str, source_line: str) -> str:
    h = hashlib.sha1(f"{task_id}|{verb}|{source_line}".encode()).hexdigest()[:12]
    return f"{task_id}:{verb}:{h}"


def _read_all() -> list[WakeEntry]:
    p = _path()
    if not p.exists():
        return []
    out: list[WakeEntry] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        out.append(WakeEntry(**d))
    return out


def existing_keys() -> set[str]:
    return {e.key for e in _read_all()}


def enqueue(task_id: str, verb: str, detail: str, source_line: str) -> bool:
    """Append a wake unless its dedupe key is already present. Returns True if added."""
    home.ensure_home()
    key = dedupe_key(task_id, verb, source_line)
    if key in existing_keys():
        return False
    entry = WakeEntry(
        task_id=task_id, verb=verb, detail=detail, key=key, ts=time.time(), delivered=False
    )
    # Single O_APPEND write keeps concurrent enqueues from interleaving.
    data = (json.dumps(entry.to_json()) + "\n").encode("utf-8")
    fd = os.open(_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return True


def undelivered() -> list[WakeEntry]:
    return [e for e in _read_all() if not e.delivered]


def mark_delivered(keys: set[str]) -> None:
    """Rewrite the queue atomically, flipping delivered=True for the given keys."""
    p = _path()
    if not p.exists():
        return
    entries = _read_all()
    lines = []
    for e in entries:
        d = e.to_json()
        if e.key in keys:
            d["delivered"] = True
        lines.append(json.dumps(d))
    tmp = p.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.replace(tmp, p)

"""On-disk task store: atomic meta writes, append-only logs (T-02, DESIGN §2.9).

All coxswain state is files so any component can die and restart without loss
(DESIGN P3). meta.json is written atomically (tmp+rename); status.log and
cost.jsonl are append-only with single O_APPEND writes so concurrent writers
(worker + control plane) never interleave a partial line.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import home
from .model import CostEntry, TaskMeta

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, maxlen: int = 24) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return s[:maxlen].strip("-") or "task"


def new_task_id(repo: str, hint: str) -> str:
    """Repo-qualified, time-stamped id: <repo>-<slug>-<yymmddHHMM> (relay pattern)."""
    stamp = time.strftime("%y%m%d%H%M", time.localtime())
    return f"{slugify(repo)}-{slugify(hint)}-{stamp}"


def task_state_dir(task_id: str) -> Path:
    return home.state_dir() / task_id


def task_data_dir(task_id: str) -> Path:
    return home.data_dir() / task_id


def _meta_path(task_id: str) -> Path:
    return task_state_dir(task_id) / "meta.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_meta(meta: TaskMeta) -> None:
    _atomic_write(_meta_path(meta.id), json.dumps(meta.to_json(), indent=2))


def load_meta(task_id: str) -> TaskMeta:
    return TaskMeta.from_json(json.loads(_meta_path(task_id).read_text(encoding="utf-8")))


def meta_exists(task_id: str) -> bool:
    return _meta_path(task_id).exists()


def list_task_ids() -> list[str]:
    sd = home.state_dir()
    if not sd.exists():
        return []
    return sorted(p.name for p in sd.iterdir() if p.is_dir() and (p / "meta.json").exists())


def _append_line(path: Path, line: str) -> None:
    """Append exactly one newline-terminated line with a single O_APPEND write.

    A lone os.write of one bytes object is atomic for pipes/regular files under
    PIPE_BUF, which keeps concurrent appenders from interleaving (DESIGN §2.9).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (line.rstrip("\n") + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def append_status(task_id: str, line: str) -> None:
    _append_line(task_data_dir(task_id) / "status.log", line)


def append_cost(task_id: str, entry: CostEntry) -> None:
    _append_line(task_state_dir(task_id) / "cost.jsonl", json.dumps(entry.to_json()))


def read_cost(task_id: str) -> list[CostEntry]:
    p = task_state_dir(task_id) / "cost.jsonl"
    if not p.exists():
        return []
    out: list[CostEntry] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        out.append(CostEntry(**d))
    return out


def cost_total(task_id: str) -> tuple[int, int, float | None]:
    """(tokens_in, tokens_out, cost_usd or None if any entry lacked a cost)."""
    entries = read_cost(task_id)
    tin = sum(e.tokens_in for e in entries)
    tout = sum(e.tokens_out for e in entries)
    costs = [e.cost_usd for e in entries]
    total = sum(c for c in costs if c is not None) if all(c is not None for c in costs) else None
    return tin, tout, total


def append_history(record: dict) -> None:
    """Append one completed-task record to state/history.jsonl (D1 trend log)."""
    home.ensure_home()
    _append_line(home.state_dir() / "history.jsonl", json.dumps(record))


def read_history(limit: int = 200) -> list[dict]:
    """The last `limit` completed-task records, oldest→newest."""
    p = home.state_dir() / "history.jsonl"
    if not p.exists():
        return []
    rows: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows[-limit:]

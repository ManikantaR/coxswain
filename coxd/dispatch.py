"""Dispatch a task into the store (DESIGN-V35). Manual only — nothing auto-runs.

Creates the worktree + a queued task; the `coxd serve` runner picks it up and
runs the loop. Keeps dispatch cheap and side-effect-light so it can be called
from a CLI or (later) the board's + Dispatch button.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import rules
import store
import worktree


def dispatch(repo_path: str | Path, brief: str) -> str:
    repo_path = Path(repo_path).expanduser().resolve()
    if not (repo_path / ".git").exists():
        raise ValueError(f"{repo_path} is not a git repo")
    repo_name = repo_path.name
    slug = re.sub(r"[^a-z0-9]+", "-", brief.lower())[:28].strip("-") or "task"
    task_id = f"{repo_name}-{slug}-{int(time.time())}"
    wt = worktree.create(repo_path, task_id)
    rb = rules.rules_block(repo_name)
    if rb:  # compounding lessons first, so the implementer reads them up front (P1)
        brief = f"{rb}\n\n{brief}"
    store.create_task(task_id, repo_name, brief, str(wt), repo_path=str(repo_path))
    return task_id

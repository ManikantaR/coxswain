"""Dispatch a task: render brief, create worktree, spawn worker (T-06/T-07).

Enforces the pre-spawn guardrails (DESIGN §4): not PAUSED, under the worker
cap, model pinned. The `inline` path never reaches here (the orchestrator
handles it in-session); this module handles `quick` and `full`.
"""

from __future__ import annotations

from pathlib import Path

from . import home, models, store, worktree
from .lanes import get_lane
from .model import DispatchPath, TaskMeta, TaskState

MAX_WORKERS = 3
HARD_MAX_WORKERS = 5

_ACTIVE = {TaskState.WORKING, TaskState.GATING, TaskState.FIXING}
_TEMPLATE = Path(__file__).parent / "templates" / "brief.md"


class DispatchError(RuntimeError):
    pass


def active_worker_count() -> int:
    n = 0
    for tid in store.list_task_ids():
        if store.load_meta(tid).state in _ACTIVE:
            n += 1
    return n


def render_brief(*, title: str, body: str, lane: str, worktree_path: Path, task_id: str) -> str:
    tpl = _TEMPLATE.read_text(encoding="utf-8")
    return tpl.format(
        title=title,
        body=body,
        lane=lane,
        worktree=worktree_path,
        status_log=store.task_data_dir(task_id) / "status.log",
        evidence_dir=store.task_data_dir(task_id) / "evidence",
    )


def dispatch(
    *,
    repo_path: Path,
    title: str,
    body: str,
    path: DispatchPath,
    lane: str = "claude",
    repo_name: str | None = None,
    model_override: str | None = None,
) -> TaskMeta:
    """Create and spawn a task. Returns its persisted meta (state=working)."""
    home.ensure_home()
    if path is DispatchPath.INLINE:
        raise DispatchError("inline tasks are handled in-session, not dispatched")
    if home.is_paused():
        raise DispatchError("coxswain is PAUSED (state/PAUSED) — dispatch refused")
    if active_worker_count() >= HARD_MAX_WORKERS:
        raise DispatchError(f"hard worker cap {HARD_MAX_WORKERS} reached")
    if active_worker_count() >= MAX_WORKERS:
        raise DispatchError(f"worker cap {MAX_WORKERS} reached — finish or pause a task first")

    repo_path = repo_path.expanduser().resolve()
    repo = repo_name or repo_path.name
    task_id = store.new_task_id(repo, title)

    # --model wins (e.g. opus:high for a hard task); else the lane's pinned default.
    if model_override:
        model = models.parse_spec(model_override)
    else:
        model = models.resolve("implementer", repo_path=repo_path, lane=lane)  # crashes if unpinned
    wt = worktree.create(repo_path, task_id)

    brief_text = render_brief(
        title=title, body=body, lane=lane, worktree_path=wt.path, task_id=task_id
    )
    brief_path = store.task_data_dir(task_id) / "brief.md"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(brief_text, encoding="utf-8")

    meta = TaskMeta(
        id=task_id,
        repo=repo,
        worktree=str(wt.path),
        branch=wt.branch,
        lane=lane,
        model=f"{model.model}:{model.effort}",
        path=path,
        state=TaskState.QUEUED,
    )
    store.save_meta(meta)

    log_path = store.task_data_dir(task_id) / "worker.log"
    pid_path = store.task_state_dir(task_id) / "pid"
    get_lane(lane).spawn(
        brief_path=brief_path, worktree=wt.path, model=model, log_path=log_path, pid_path=pid_path
    )

    meta = _with_state(meta, TaskState.WORKING)
    store.save_meta(meta)
    store.append_status(task_id, f"working: dispatched on {lane} ({model.model})")
    return meta


def _with_state(meta: TaskMeta, state: TaskState) -> TaskMeta:
    from dataclasses import replace

    return replace(meta, state=state)

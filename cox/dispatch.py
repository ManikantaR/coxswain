"""Dispatch a task: render brief, create worktree, spawn worker (T-06/T-07).

Enforces the pre-spawn guardrails (DESIGN §4): not PAUSED, under the worker
cap, model pinned. The `inline` path never reaches here (the orchestrator
handles it in-session); this module handles `quick` and `full`.
"""

from __future__ import annotations

from pathlib import Path

from . import home, models, repos, store, worktree
from .lanes import get_lane
from .model import DispatchPath, TaskMeta, TaskState

MAX_WORKERS = 3
HARD_MAX_WORKERS = 5

_ACTIVE = {TaskState.PLANNING, TaskState.WORKING, TaskState.GATING, TaskState.FIXING}
_TEMPLATE = Path(__file__).parent / "templates" / "brief.md"


class DispatchError(RuntimeError):
    pass


def active_worker_count() -> int:
    n = 0
    for tid in store.list_task_ids():
        if store.load_meta(tid).state in _ACTIVE:
            n += 1
    return n


def render_brief(
    *, title: str, body: str, lane: str, worktree_path: Path, task_id: str,
    repo: str = "", with_plan: bool = False,
) -> str:
    from . import acceptance, rules

    tpl = _TEMPLATE.read_text(encoding="utf-8")
    if with_plan:
        body = (
            f"{body}\n\n## Approved plan\n"
            "An architect drafted an implementation plan at `plan.md` in the worktree "
            "root. Read it first and follow it; deviate only where it is clearly wrong."
        )
    ac = acceptance.criteria_block(task_id)  # definition-of-done + self-check (P2)
    if ac:
        body = f"{body}\n\n{ac}"
    rb = rules.rules_block(repo) if repo else ""
    if rb:  # compounding lessons first, so the implementer reads them up front (P1)
        body = f"{rb}\n\n{body}"
    return tpl.format(
        title=title,
        body=body,
        lane=lane,
        worktree=worktree_path,
        status_log=store.task_data_dir(task_id) / "status.log",
        evidence_dir=store.task_data_dir(task_id) / "evidence",
    )


def spawn_implementer(
    meta: TaskMeta, *, title: str, body: str, with_plan: bool = False
) -> TaskMeta:
    """Render the implementer brief, spawn the worker, move to WORKING.

    Shared by a plain dispatch and by the plan phase (plan.py) once the plan is
    approved — the implement slot is welded to one provider (DESIGN-VNEXT D15),
    so both paths spawn the same lane/model recorded on the task."""
    task_id = meta.id
    model = models.parse_spec(meta.model)
    brief_text = render_brief(
        title=title, body=body, lane=meta.lane, worktree_path=Path(meta.worktree),
        task_id=task_id, repo=meta.repo, with_plan=with_plan,
    )
    brief_path = store.task_data_dir(task_id) / "brief.md"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(brief_text, encoding="utf-8")

    log_path = store.task_data_dir(task_id) / "worker.log"
    pid_path = store.task_state_dir(task_id) / "pid"
    get_lane(meta.lane).spawn(
        brief_path=brief_path, worktree=Path(meta.worktree), model=model,
        log_path=log_path, pid_path=pid_path,
    )
    meta = _with_state(meta, TaskState.WORKING)
    store.save_meta(meta)
    store.append_status(task_id, f"working: dispatched on {meta.lane} ({model.model})")
    return meta


def dispatch(
    *,
    repo_path: Path,
    title: str,
    body: str,
    path: DispatchPath,
    lane: str = "claude",
    repo_name: str | None = None,
    model_override: str | None = None,
    review_lane: str | None = None,
    review_model: str | None = None,
    plan_lane: str | None = None,
    plan_model: str | None = None,
    plan_approval: bool = False,
    acceptance: list[str] | None = None,
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
    if not repos.is_trusted(repo_path):
        raise DispatchError(
            f"repo {repo_path} was cloned by coxswain but not yet trusted — "
            "confirm it in the UI (or `cox repos trust`) before dispatching"
        )
    repo = repo_name or repo_path.name
    task_id = store.new_task_id(repo, title)

    # --model wins (e.g. opus:high for a hard task); else the lane's pinned default.
    if model_override:
        model = models.parse_spec(model_override)
    else:
        model = models.resolve("implementer", repo_path=repo_path, lane=lane)  # crashes if unpinned
    wt = worktree.create(repo_path, task_id)
    store.task_data_dir(task_id).mkdir(parents=True, exist_ok=True)

    meta = TaskMeta(
        id=task_id,
        repo=repo,
        worktree=str(wt.path),
        branch=wt.branch,
        lane=lane,
        model=f"{model.model}:{model.effort}",
        path=path,
        state=TaskState.QUEUED,
        review_lane=review_lane or None,
        review_model=review_model or None,
        plan_lane=plan_lane or None,
        plan_model=plan_model or None,
        plan_approval=bool(plan_approval),
    )
    # Keep the raw task text for the (possibly deferred) implementer brief.
    store.save_meta(meta)
    _save_task_text(task_id, title, body)
    if acceptance:
        from . import acceptance as accept

        accept.save_criteria(task_id, acceptance)

    # Plan phase first (DESIGN-VNEXT D14): an architect drafts plan.md, then the
    # implementer runs. Without a plan slot, dispatch straight to the implementer.
    if plan_lane:
        from . import plan

        meta = _with_state(meta, TaskState.PLANNING)
        store.save_meta(meta)
        store.append_status(task_id, f"planning: architect drafting on {plan_lane}")
        plan.start(task_id)
        return meta
    return spawn_implementer(meta, title=title, body=body)


def _save_task_text(task_id: str, title: str, body: str) -> None:
    import json

    (store.task_data_dir(task_id) / "task.json").write_text(
        json.dumps({"title": title, "body": body}), encoding="utf-8"
    )


def load_task_text(task_id: str) -> tuple[str, str]:
    """The original title/body, for a brief rendered after the plan phase."""
    import json

    p = store.task_data_dir(task_id) / "task.json"
    if not p.exists():
        m = store.load_meta(task_id)
        return m.id, ""
    d = json.loads(p.read_text(encoding="utf-8"))
    return d.get("title", ""), d.get("body", "")


def _with_state(meta: TaskMeta, state: TaskState) -> TaskMeta:
    from dataclasses import replace

    return replace(meta, state=state)

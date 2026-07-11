"""Ship + merge + teardown (T-11/T-12, DESIGN §2.3).

Push -> PR (body = gate report + evidence excerpt + cost total) -> pr_open,
schedule the CI check for the watcher. Merge on the captain's word. Teardown is
fail-closed (worktree.remove).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from . import store, worktree
from .model import NeedsHumanReason, TaskMeta, TaskState
from .repoconfig import load_repo_config
from .scm.base import PrError, PushRejected, get_scm


def _pr_body(task_id: str) -> str:
    gate = store.task_data_dir(task_id) / "gate.json"
    ev = store.task_data_dir(task_id) / "evidence" / "summary.md"
    tin, tout, cost = store.cost_total(task_id)
    cost_str = f"${cost:.2f}" if cost is not None else "unknown"
    lines = [
        f"Dispatched via coxswain (`{task_id}`).",
        "",
        f"**Cost:** {tin} in / {tout} out tokens · {cost_str}",
    ]
    if gate.exists():
        lines += ["", "**Gate:** " + gate.read_text(encoding="utf-8").strip()]
    if ev.exists():
        lines += ["", "**Evidence summary:**", ev.read_text(encoding="utf-8").strip()]
    lines += ["", "🤖 dispatched via coxswain"]
    return "\n".join(lines)


def ship(task_id: str, repo_path: Path, title: str) -> TaskMeta:
    """Push the branch and open a PR. Typed failures -> needs-human."""
    meta = store.load_meta(task_id)
    cfg = load_repo_config(Path(meta.worktree))
    scm = get_scm(cfg.scm)
    try:
        scm.push(Path(meta.worktree), meta.branch)
    except PushRejected as e:
        meta = replace(meta, state=TaskState.NEEDS_HUMAN, reason=NeedsHumanReason.PUSH_REJECTED)
        store.save_meta(meta)
        store.append_status(task_id, f"failed: {e}")
        return meta
    try:
        url = scm.create_pr(repo_path, meta.branch, title, _pr_body(task_id), cfg.target_branch)
    except PrError as e:
        meta = replace(meta, state=TaskState.NEEDS_HUMAN, reason=NeedsHumanReason.PR_ERROR)
        store.save_meta(meta)
        store.append_status(task_id, f"failed: {e}")
        return meta
    meta = replace(meta, state=TaskState.PR_OPEN, pr_url=url)
    store.save_meta(meta)
    store.append_status(task_id, f"pr-ready: {url}")
    return meta


def _record_history(meta: TaskMeta) -> None:
    """Log a landed task for the cross-task cycle-time / fix-round trend (D1)."""
    import time

    tin, tout, cost = store.cost_total(meta.id)
    fix_rounds = sum(1 for e in store.read_cost(meta.id) if "fix" in e.phase)
    cycle = max(0.0, time.time() - (meta.dispatched_at or time.time()))
    store.append_history({
        "id": meta.id, "repo": meta.repo, "lane": meta.lane, "ts": time.time(),
        "cycle_secs": int(cycle), "fix_rounds": fix_rounds,
        "tokens": tin + tout, "cost_usd": cost,
    })


def merge(task_id: str, repo_path: Path) -> TaskMeta:
    """Merge on the captain's word, then attempt fail-closed teardown."""
    meta = store.load_meta(task_id)
    if not meta.pr_url:
        raise RuntimeError(f"{task_id}: no PR to merge")
    cfg = load_repo_config(Path(meta.worktree))
    scm = get_scm(cfg.scm)
    scm.merge(repo_path, meta.pr_url, squash=True)
    meta = replace(meta, state=TaskState.LANDED)
    store.save_meta(meta)
    store.append_status(task_id, "done: merged")
    _record_history(meta)
    try:
        worktree.remove(
            repo_path,
            worktree.Worktree(path=Path(meta.worktree), branch=meta.branch),
            cfg.target_branch,
        )
    except worktree.UnlandedWorkError:
        pass  # keep the worktree; landed check will pass once the remote catches up
    return meta

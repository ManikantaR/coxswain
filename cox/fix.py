"""Fix rounds via resumed sessions (T-10, DESIGN §2.4, P4).

A fix round resumes the SAME implementer session so feedback costs only the
delta, not a repo re-read (relay's #1 token burner). Max MAX_FIX_ROUNDS; the
next red gate after that is needs-human(gate-red).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from . import store
from .lanes import get_lane
from .model import NeedsHumanReason, TaskMeta, TaskState

MAX_FIX_ROUNDS = 2


class FixCapReached(RuntimeError):
    pass


def assemble_feedback(task_id: str, notes: str | None = None) -> str:
    parts: list[str] = []
    fb = store.task_data_dir(task_id) / "feedback.md"
    if fb.exists():
        parts.append(fb.read_text(encoding="utf-8"))
    review = store.task_data_dir(task_id) / "review.json"
    if review.exists():
        parts.append("# Review findings\n" + review.read_text(encoding="utf-8"))
    if notes:
        parts.append(f"# Captain notes\n{notes}")
    parts.append(
        "\nAddress the above in this same worktree, keep the fix minimal, re-run "
        "tests, then append `done:` to the status log. Do not push."
    )
    return "\n\n".join(parts)


def fix(task_id: str, notes: str | None = None) -> TaskMeta:
    """Start a resumed fix round. Raises FixCapReached past the cap."""
    meta = store.load_meta(task_id)
    if meta.fix_rounds >= MAX_FIX_ROUNDS:
        meta = replace(meta, state=TaskState.NEEDS_HUMAN, reason=NeedsHumanReason.GATE_RED)
        store.save_meta(meta)
        raise FixCapReached(
            f"{task_id}: {MAX_FIX_ROUNDS} fix rounds exhausted -> needs-human(gate-red)"
        )
    if not meta.session_id:
        raise RuntimeError(f"{task_id}: no session_id to resume (lane={meta.lane})")

    feedback = assemble_feedback(task_id, notes)
    worktree = Path(meta.worktree)
    log_path = store.task_data_dir(task_id) / "worker.log"
    pid_path = store.task_state_dir(task_id) / "pid"
    get_lane(meta.lane).resume(
        session_id=meta.session_id,
        feedback=feedback,
        worktree=worktree,
        log_path=log_path,
        pid_path=pid_path,
    )
    meta = replace(meta, state=TaskState.FIXING, fix_rounds=meta.fix_rounds + 1)
    store.save_meta(meta)
    store.append_status(task_id, f"working: fix round {meta.fix_rounds} (resumed session)")
    return meta

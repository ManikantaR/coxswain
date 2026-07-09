"""Plan phase (DESIGN-VNEXT D14/D16): an architect drafts plan.md up front.

The plan slot is stateless — the architect reads the brief (read-only, like the
reviewer), emits a plan, and is gone. plan.md is a FILE handoff to the
implementer, so the architect may be any lane/model without breaking the welded
implement+fix resume (D15). With plan_approval on, the task parks at
needs-human(plan-review) until the captain okays it; otherwise the implementer
starts straight away.

Flow: dispatch → PLANNING (architect spawned) → [architect exits] → finalize()
captures plan.md → approval? park : spawn implementer → WORKING → normal loop.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from . import models, proc, store
from .lanes.codex import parse_codex_jsonl
from .model import NeedsHumanReason, TaskState
from .review import _final_text, _review_argv, _wait_for_exit

# Structured handoff shape (mattpocock/handoff pattern): a DIFFERENT engineer,
# with no shared session, executes this — so the plan is the whole context they
# get. Reference repo paths rather than pasting code.
_HANDOFF_SECTIONS = (
    "## Approach — the strategy in 2-4 sentences.\n"
    "## Files to touch — bullet list of paths and what changes in each.\n"
    "## Key decisions — each decision and WHY (the non-obvious choices).\n"
    "## Assumptions — what you took as given; call out anything uncertain.\n"
    "## How to verify — the exact commands/checks that prove it works.\n"
    "## Open questions — anything the implementer or captain must resolve (or 'none')."
)

_STUB_PLAN = (
    "# Plan (stub)\n\n## Approach\nMake the change described in the brief.\n\n"
    "## Files to touch\n- as needed\n\n## How to verify\n- tests pass\n"
)

_PROMPT = (
    "You are the ARCHITECT. Read the task and the repository (read-only) and write an "
    "implementation plan that a SEPARATE engineer — with none of your context — will "
    "execute. Be concrete and reference repo paths; do not paste large code. Do NOT edit "
    "anything. Output ONLY markdown with exactly these sections, and include EVERY section "
    "— never omit 'Open questions'; write 'none' explicitly if there are none:\n"
    + _HANDOFF_SECTIONS
)


class PlanError(RuntimeError):
    pass


def build_plan_prompt(title: str, body: str) -> str:
    return f"# Task\n{title}\n\n{body}\n\n# Instructions\n{_PROMPT}\n"


def _plan_spec(meta: object, lane: str) -> models.ModelSpec:
    model = getattr(meta, "plan_model", None)
    return models.parse_spec(model) if model else models.resolve("implementer", lane=lane)


def start(task_id: str) -> None:
    """Spawn the architect for a PLANNING task (lane-aware, read-only)."""
    meta = store.load_meta(task_id)
    worktree = Path(meta.worktree)
    lane = meta.plan_lane or meta.lane
    title, body = _task_text(task_id)
    prompt = build_plan_prompt(title, body)

    if lane == "stub":  # tokenless path: canned plan, architect is synchronous
        (worktree / "plan.md").write_text(_STUB_PLAN, encoding="utf-8")
        store.append_status(task_id, "plan drafted (stub)")
        return

    log_path = store.task_data_dir(task_id) / "plan.log"
    pid_path = store.task_state_dir(task_id) / "plan.pid"
    argv = _review_argv(lane, _plan_spec(meta, lane), prompt, worktree)  # same read-only shape
    proc.spawn_detached(argv, log_path=log_path, pid_path=pid_path, cwd=worktree)


def finalize(task_id: str) -> object:
    """Called once the architect exits: capture plan.md, then gate on approval."""
    meta = store.load_meta(task_id)
    worktree = Path(meta.worktree)
    lane = meta.plan_lane or meta.lane
    plan_path = worktree / "plan.md"

    if lane != "stub":  # capture the architect's output into plan.md (+ record cost)
        log_path = store.task_data_dir(task_id) / "plan.log"
        pid_path = store.task_state_dir(task_id) / "plan.pid"
        pid = _read_pid(pid_path)
        if pid > 0:
            _wait_for_exit(pid)
        if lane == "codex":
            rr = parse_codex_jsonl(log_path, phase="plan")
            raw, cost = rr.raw_tail, rr.cost
        else:
            from .lanes.claude import parse_stream_json

            raw, cost = _final_text(log_path), parse_stream_json(log_path, phase="plan").cost
        plan_path.write_text(raw or "(architect produced no plan)", encoding="utf-8")
        if cost:
            store.append_cost(task_id, cost)

    if meta.plan_approval:
        parked = replace(meta, state=TaskState.NEEDS_HUMAN, reason=NeedsHumanReason.PLAN_REVIEW)
        store.save_meta(parked)
        store.append_status(task_id, "needs-human: plan-review (approve to implement)")
        return parked
    return _proceed(task_id)


def approve(task_id: str) -> object:
    """Captain okays a parked plan → the implementer starts."""
    meta = store.load_meta(task_id)
    if not (meta.state is TaskState.NEEDS_HUMAN and meta.reason is NeedsHumanReason.PLAN_REVIEW):
        raise PlanError(f"{task_id} has no plan awaiting approval")
    store.append_status(task_id, "plan approved by captain")
    return _proceed(task_id)


def _proceed(task_id: str) -> object:
    from . import dispatch

    meta = store.load_meta(task_id)
    title, body = _task_text(task_id)
    return dispatch.spawn_implementer(meta, title=title, body=body, with_plan=True)


def _task_text(task_id: str) -> tuple[str, str]:
    from . import dispatch

    return dispatch.load_task_text(task_id)


def _read_pid(pid_path: Path) -> int:
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return -1

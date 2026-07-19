"""The coxd one-task loop (DESIGN-V35).

One task = one async function; coxd is the SINGLE state owner (every transition
goes through store.py). worker -> gate -> (resumed fix loop) -> one review pass ->
terminal (pr_ready = needs-you-to-merge, or needs_human with a typed reason).
Deterministic gate before the paid review; resume-for-fix; a review INFRA error
is a typed retryable state, never cached as a verdict (the Run-B fix).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import gate
import lane
import registry
import store
import worktree

MAX_FIX = 2


async def run_task(task_id: str, worker_model: str = "claude-sonnet-5",
                   review_model: str = "claude-opus-4-8") -> str:
    t = store.get_task(task_id)
    if t is None:
        raise ValueError(f"unknown task {task_id}")
    wt, repo, brief = Path(t["worktree"]), t["repo"], t["brief"]

    def emit(kind: str, data: dict) -> None:
        store.append_event(task_id, kind, data)

    # --- provision: install deps so the worker + gate run the repo's REAL checks
    store.set_state(task_id, "provisioning")
    prov = await asyncio.to_thread(worktree.provision, wt)  # off the event loop
    emit("provision", prov)

    # --- implement ---------------------------------------------------------
    store.set_state(task_id, "working")
    wr = await lane.run_worker(wt, brief, worker_model, emit)
    store.set_session(task_id, wr.session_id)
    store.add_cost(task_id, wr.cost)
    if wr.is_error or not wr.session_id:
        return _terminal(task_id, "needs_human", "worker-error")

    # --- gate (+ resumed fix loop) -----------------------------------------
    entry = registry.get_or_scout(repo, wt)
    fixes = 0
    while True:
        store.set_state(task_id, "gating")
        g = gate.run_gate(wt, entry)
        emit("gate", g)
        if g["passed"]:
            break
        if fixes >= MAX_FIX:
            return _terminal(task_id, "needs_human", "gate-red")
        fixes += 1
        await _resume_fix(task_id, wt, worker_model, emit,
                          f"The gate failed at {g['failing']}:\n{g.get('reason', '')}\n"
                          "Fix it minimally, re-run the check, and commit. Do not push.")

    # --- one review pass ---------------------------------------------------
    store.set_state(task_id, "reviewing")
    r = await lane.review(gate.diff(wt), review_model, emit)
    store.add_cost(task_id, r.cost)
    emit("review", {"outcome": r.outcome, "verdict": r.verdict, "findings": r.findings})
    if r.outcome == "review-error":
        return _terminal(task_id, "needs_human", "review-error")  # retryable — NOT a verdict
    if r.verdict == "reject":
        return _terminal(task_id, "needs_human", "review-findings")
    if r.verdict == "fix" and r.findings:
        if fixes >= MAX_FIX:
            return _terminal(task_id, "needs_human", "review-findings")
        await _resume_fix(task_id, wt, worker_model, emit,
                          "Address these review findings, then re-run tests and commit "
                          "(do not push):\n" + str(r.findings))
        g = gate.run_gate(wt, entry)
        emit("gate", g)
        if not g["passed"]:
            return _terminal(task_id, "needs_human", "gate-red")

    # --- ship: control plane pushes + opens the PR (workers can't) ----------
    store.set_state(task_id, "shipping")
    import ship
    outcome, url = ship.ship(task_id)
    emit("ship", {"outcome": outcome, "pr_url": url})
    if outcome == "push-error":
        return _terminal(task_id, "needs_human", "push-rejected")
    if outcome == "pr-error":
        return _terminal(task_id, "needs_human", "pr-error")
    # pr | local -> ready for the captain's merge (the one standing human gate)
    return _terminal(task_id, "pr_ready", None)


async def _resume_fix(task_id: str, wt: Path, model: str, emit, feedback: str) -> None:
    store.set_state(task_id, "fixing")
    sid = store.get_task(task_id)["session_id"]  # resume the SAME session (cheap)
    wr = await lane.run_worker(wt, feedback, model, emit, resume=sid)
    store.add_cost(task_id, wr.cost)
    if wr.session_id:
        store.set_session(task_id, wr.session_id)


def _terminal(task_id: str, state: str, reason: str | None) -> str:
    store.set_state(task_id, state, reason)
    if state in ("pr_ready", "needs_human"):  # the AFK ping
        import notify
        t = store.get_task(task_id)
        if state == "pr_ready":
            notify.notify_async("coxd · ready to merge", f"{task_id} ({t['repo']})")
        else:
            notify.notify_async(f"coxd · needs you ({reason})",
                                f"{task_id} ({t['repo']})", priority="high")
    return state

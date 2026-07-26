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
                   review_model: str = "claude-opus-4-8",
                   effort: str = "medium") -> str:
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
    wr = await lane.run_worker(wt, brief, worker_model, emit, effort=effort)
    store.set_session(task_id, wr.session_id)
    store.add_cost(task_id, wr.cost)
    store.set_summary(task_id, wr.summary)
    if wr.is_error or not wr.session_id:
        return _terminal(task_id, "needs_human", "worker-error")

    entry = registry.get_or_scout(repo, wt)
    return await _gate_review_ship(task_id, wt, repo, entry, worker_model,
                                   review_model, effort, emit)


async def _gate_review_ship(task_id: str, wt: Path, repo: str, entry: dict,
                            worker_model: str, review_model: str, effort: str,
                            emit) -> str:
    """The tail shared by a fresh implement (run_task) and a human-resumed retry
    (resume_task): gate (+ resumed fix loop) -> one review pass -> ship -> CI watch."""
    t = store.get_task(task_id)
    fixes = 0
    while True:
        store.set_state(task_id, "gating")
        g = gate.run_gate(wt, entry)
        emit("gate", g)
        if g["passed"]:
            store.set_gate_steps(task_id, g.get("steps"), g.get("test_output"))
            break
        if fixes >= MAX_FIX:
            return _terminal(task_id, "needs_human", "gate-red")
        fixes += 1
        feedback = (f"The gate failed at {g['failing']}:\n{g.get('reason', '')}\n"
                   "Fix it minimally, re-run the check, and commit. Do not push.")
        emit("fix-trigger", {"source": "gate", "failing": g["failing"],
                             "reason": g.get("reason", ""), "feedback": feedback})
        await _resume_fix(task_id, wt, worker_model, emit, feedback, effort=effort)

    # --- one review pass ---------------------------------------------------
    store.set_state(task_id, "reviewing")
    r = await lane.review(gate.diff(wt), review_model, emit, effort=effort)
    store.add_cost(task_id, r.cost)
    emit("review", {"outcome": r.outcome, "verdict": r.verdict, "findings": r.findings})
    if r.outcome == "reviewed":
        store.set_review(task_id, r.verdict, r.findings)
    if r.outcome == "review-error":
        return _terminal(task_id, "needs_human", "review-error")  # retryable — NOT a verdict
    if r.verdict == "reject":
        return _terminal(task_id, "needs_human", "review-findings")
    if r.verdict == "fix" and r.findings:
        if fixes >= MAX_FIX:
            return _terminal(task_id, "needs_human", "review-findings")
        feedback = ("Address these review findings, then re-run tests and commit "
                   "(do not push):\n" + str(r.findings))
        emit("fix-trigger", {"source": "review", "findings": r.findings, "feedback": feedback})
        await _resume_fix(task_id, wt, worker_model, emit, feedback, effort=effort)
        g = gate.run_gate(wt, entry)
        emit("gate", g)
        if not g["passed"]:
            return _terminal(task_id, "needs_human", "gate-red")
        store.set_gate_steps(task_id, g.get("steps"))

    # --- ship: control plane pushes + opens the PR (workers can't) ----------
    store.set_state(task_id, "shipping")
    import ship
    outcome, url = ship.ship(task_id)
    emit("ship", {"outcome": outcome, "pr_url": url})
    if outcome == "push-error":
        return _terminal(task_id, "needs_human", "push-rejected")
    if outcome == "pr-error":
        return _terminal(task_id, "needs_human", "pr-error")

    # --- CI watch + flake triage: don't hand the human a PR that's still red
    # for a reason coxd could have resolved itself (the recurring hook-timeout
    # e2e flake hit ~half of tonight's PRs and needed a human retry every time).
    if outcome == "pr" and url:
        import ci_triage
        slug = ci_triage.repo_slug(t["repo_path"])
        if slug:
            store.set_state(task_id, "ci-watching")
            emit("ci-watch", {"pr_url": url})
            ci_outcome, detail = await asyncio.to_thread(
                ci_triage.watch_and_triage, url, slug, wt)
            emit("ci-triage", {"outcome": ci_outcome, "detail": detail})
            if ci_outcome == "needs_human":
                return _terminal(task_id, "needs_human", f"ci-red: {detail}")
    # pr | local -> ready for the captain's merge (the one standing human gate)
    return _terminal(task_id, "pr_ready", None)


async def resume_task(task_id: str, note: str, worker_model: str = "claude-sonnet-5",
                      review_model: str = "claude-opus-4-8",
                      effort: str = "medium") -> str:
    """Board-triggered resume: a human gives the SAME worker session a note (e.g.
    'the gate-red was a flaky port conflict, just retry' or 'use approach X instead'),
    then re-runs the shared gate->review->ship tail. Distinct from _resume_fix, which
    is the automatic in-loop version — this one is for a task that already stopped at
    needs_human and a human is choosing to unstick it, not the loop retrying itself."""
    t = store.get_task(task_id)
    if t is None:
        raise ValueError(f"unknown task {task_id}")
    wt, repo = Path(t["worktree"]), t["repo"]

    def emit(kind: str, data: dict) -> None:
        store.append_event(task_id, kind, data)

    emit("human-note", {"note": note})
    await _resume_fix(task_id, wt, worker_model, emit, note, effort=effort)
    entry = registry.get_or_scout(repo, wt)
    return await _gate_review_ship(task_id, wt, repo, entry, worker_model,
                                   review_model, effort, emit)


async def _resume_fix(task_id: str, wt: Path, model: str, emit, feedback: str,
                      effort: str = "medium") -> None:
    store.set_state(task_id, "fixing")
    sid = store.get_task(task_id)["session_id"]  # resume the SAME session (cheap)
    wr = await lane.run_worker(wt, feedback, model, emit, resume=sid, effort=effort)
    store.add_cost(task_id, wr.cost)
    store.set_summary(task_id, wr.summary)
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

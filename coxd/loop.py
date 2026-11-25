"""The coxd one-task loop (DESIGN-V35).

One task = one async function; coxd is the SINGLE state owner (every transition
goes through store.py). worker -> gate -> (resumed fix loop) -> one review pass ->
terminal (pr_ready = needs-you-to-merge, or needs_human with a typed reason).
Deterministic gate before the paid review; resume-for-fix; a review INFRA error
is a typed retryable state, never cached as a verdict (the Run-B fix).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import agy_handoff
import autofix
import gate
import lane
import notify
import quota
import registry
import roles
import sandbox
import store
import worktree

MAX_FIX = 2
MAX_REVIEW_ROUNDS = 3  # completed reviews per task, across resumes/agy pickups; then needs-you
AGY_LANE = "agy-handoff"
AGY_BRANCH_PREFIX = "coxd-agy/"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


async def _plan_and_hand_off(task_id: str, t: dict, emit) -> str:
    """agy-handoff lane: a read-only Claude plan, then a `coxd-agy/<id>` branch on the
    remote cut from the base SHA, then park at waiting_agy for the human. coxd never
    runs agy; the only interface is that branch plus the board's plan endpoint."""
    wt, repo = Path(t["worktree"]), t["repo"]
    repo_path = Path(t["repo_path"]) if t.get("repo_path") else None
    store.set_state(task_id, "planning")
    cfg = roles.resolve(registry.load(repo) or {})
    emit("config", cfg)
    base = await asyncio.to_thread(_git, wt, "rev-parse", "HEAD")
    sha = base.stdout.strip()
    if base.returncode != 0 or not sha:
        return _terminal(task_id, "needs_human", "agy-base-error")
    pr = await lane.plan(t["brief"], cfg["planner_model"], emit,
                         worktree=wt, effort=cfg["planner_effort"])
    store.add_cost(task_id, pr.cost)
    if pr.quota_reset_at:
        raise quota.QuotaWait(pr.quota_reset_at, "plan")
    if pr.outcome != "planned" or not pr.plan_md:
        return _terminal(task_id, "needs_human", "plan-error")
    branch = f"{AGY_BRANCH_PREFIX}{task_id}"
    if repo_path is None:
        return _terminal(task_id, "needs_human", "agy-push-error")
    # From the main checkout (as ship.py does), a bare SHA -> new remote branch.
    push = await asyncio.to_thread(_git, repo_path, "push", "origin",
                                   f"{sha}:refs/heads/{branch}")
    if push.returncode != 0:
        emit("agy-error", {"stage": "push", "err": push.stderr[-300:]})
        return _terminal(task_id, "needs_human", "agy-push-error")
    store.set_plan(task_id, pr.plan_md, sha)
    rnd = 1 + sum(1 for e in store.events(task_id) if e["kind"] == "plan")
    issue, depends_on = agy_handoff.issue_and_deps(t.get("brief"))
    emit("plan", {
        "plan_md": pr.plan_md,
        "branch": branch,
        "base_sha": sha,
        "round": rnd,
        "files": pr.files,
        "issue": issue,
        "depends_on": depends_on,
    })
    store.set_state(task_id, "waiting_agy", f"run agy-next {task_id}")
    return "waiting_agy"


PLAN_BRIEF_NOTE = (
    "\n\n# Plan\nA planner read the repo and wrote this plan. Follow this plan; deviate "
    "only if it is wrong, and say why.\n\n{plan}\n")


def _with_plan(brief: str, plan_md: str | None) -> str:
    return brief if not plan_md else brief + PLAN_BRIEF_NOTE.format(plan=plan_md)


async def _plan_first(task_id: str, wt: Path, brief: str, cfg: dict, emit) -> str | None:
    """Read-only planner pass (the agy-lane `lane.plan`) before the worker. Its cost goes
    to the ledger and a `plan` event; a failed plan is not fatal — the worker just runs
    without one. A usage-limit cut-off parks the task like any other stage."""
    pr = await lane.plan(brief, cfg["planner_model"], emit, worktree=wt,
                         effort=cfg["planner_effort"])
    store.add_cost(task_id, pr.cost)
    if pr.quota_reset_at:
        raise quota.QuotaWait(pr.quota_reset_at, "plan")
    if pr.outcome != "planned" or not pr.plan_md:
        return None
    t = store.get_task(task_id)
    issue, depends_on = agy_handoff.issue_and_deps(t.get("brief") if t else brief)
    emit("plan", {
        "plan_md": pr.plan_md,
        "cost": pr.cost or 0.0,
        "phase": "plan",
        "files": pr.files,
        "issue": issue,
        "depends_on": depends_on,
    })
    return pr.plan_md


async def run_task(task_id: str, worker_model: str | None = None,
                   review_model: str | None = None,
                   worker_effort: str | None = None,
                   review_effort: str | None = None) -> str:
    t = store.get_task(task_id)
    if t is None:
        raise ValueError(f"unknown task {task_id}")
    wt, repo, brief = Path(t["worktree"]), t["repo"], t["brief"]

    def emit(kind: str, data: dict) -> None:
        store.append_event(task_id, kind, data)

    if t.get("lane") == AGY_LANE:  # agy handoff: plan + branch, never implement here
        if t.get("plan_md"):  # plan already handed off: a pushed branch / Claude-fix round
            return await _agy_continue(task_id, t, emit)
        return await _plan_and_hand_off(task_id, t, emit)

    # --- provision: install deps so the worker + gate run the repo's REAL checks
    store.set_state(task_id, "provisioning")
    prov = await asyncio.to_thread(worktree.provision, wt)  # off the event loop
    emit("provision", prov)

    # --- plan first (opt-in per repo: registry `plan_first: true`) ---------
    reg = registry.load(repo) or {}
    cfg = roles.resolve(reg, worker_model=worker_model, review_model=review_model,
                        worker_effort=worker_effort, review_effort=review_effort)
    plan_md = None
    if reg.get("plan_first") is True:
        store.set_state(task_id, "planning")
        emit("config", cfg)
        plan_md = await _plan_first(task_id, wt, brief, cfg, emit)

    # --- implement ---------------------------------------------------------
    store.set_state(task_id, "working")
    worker_model, review_model = cfg["worker_model"], cfg["review_model"]
    if plan_md is None:
        emit("config", cfg)
    wr = await lane.run_worker(wt, registry.data_guard_brief(reg, _with_plan(brief, plan_md)),
                               worker_model, emit,
                               effort=cfg["worker_effort"])
    store.set_session(task_id, wr.session_id)
    store.add_cost(task_id, wr.cost)
    store.set_summary(task_id, wr.summary)
    if wr.quota_reset_at:  # cut off by the usage limit — park, don't fail (quota.py)
        raise quota.QuotaWait(wr.quota_reset_at, "implement")
    if wr.is_error or not wr.session_id:
        return _terminal(task_id, "needs_human", "worker-error")

    entry = registry.get_or_scout(repo, wt)
    return await _gate_review_ship(task_id, wt, repo, entry, worker_model,
                                   review_model, cfg["worker_effort"], emit,
                                   review_effort=cfg["review_effort"],
                                   rereview_model=cfg["rereview_model"],
                                   rereview_effort=cfg["rereview_effort"])


def _review_rounds(task_id: str) -> int:
    """Completed review passes so far (review events with outcome `reviewed`). Counted from
    the event log so the cap survives agy pickups and ordinary resumes (only a resume
    after a `review-cap` stop starts a new allowance); a review-error run
    produced no verdict and doesn't count."""
    try:
        evs = store.events(task_id)
    except Exception:
        return 0
    n = 0
    for e in evs:
        if e["kind"] == "review-cap":  # a human resumed past the cap: a fresh allowance
            n = 0
        elif e["kind"] == "review" and (e.get("data") or {}).get("outcome") == "reviewed":
            n += 1
    return n


def _review_capped(task_id: str) -> bool:
    return _review_rounds(task_id) >= MAX_REVIEW_ROUNDS


def _review_cap_stop(task_id: str, emit, findings: list | None = None) -> str:
    """Stop looping: mark the task needs-you with the open findings instead of paying
    for another round (one task needed 8)."""
    if not findings:
        t = store.get_task(task_id)
        findings = store.findings_of(t) if t else []
    emit("review-cap", {"cap": MAX_REVIEW_ROUNDS, "rounds": _review_rounds(task_id),
                        "findings": findings})
    return _terminal(task_id, "needs_human", "review-cap")


async def _autofix(task_id: str, wt: Path, entry: dict, emit) -> None:
    """Free pre-gate formatter/linter fixes, committed before the gate so a format-only
    failure never costs a Claude round. Never counts as a fix round."""
    if not registry.autofix_cmds(entry):
        return
    t = store.get_task(task_id) or {}
    try:
        res = await asyncio.to_thread(autofix.run, wt, entry, t.get("repo_path"))
    except Exception as e:  # best effort: the gate decides red, not the autofixer
        res = {"ran": [], "committed": False, "files": [], "skipped": f"error: {e}"[:200]}
    emit("autofix", res)


def _last_review(task_id: str) -> tuple[str, list] | None:
    """(reviewed head sha, findings) of the latest completed review that asked for fixes,
    or None. Used when a review runs after a fix round without in-memory state (agy
    pickup / human resume). Needs a recorded head, else the caller does a full review."""
    try:
        evs = store.events(task_id)
    except Exception:
        return None
    for e in reversed(evs):
        d = e["data"]
        if e["kind"] == "review" and d.get("outcome") == "reviewed":
            if d.get("verdict") == "fix" and d.get("head") and d.get("findings"):
                return d["head"], d["findings"]
            return None  # latest completed review wasn't a fix request
    return None


async def _gate_review_ship(task_id: str, wt: Path, repo: str, entry: dict,
                            worker_model: str, review_model: str, effort: str,
                            emit, review_effort: str = "medium",
                            rereview_model: str | None = None,
                            rereview_effort: str | None = None) -> str:
    """The tail shared by a fresh implement (run_task) and a human-resumed retry
    (resume_task): gate (+ resumed fix loop) -> one review pass -> ship -> CI watch."""
    rereview_model = rereview_model or review_model
    rereview_effort = rereview_effort or review_effort
    fixes = 0
    mode = registry.fix_mode(entry)
    fresh = mode == "fresh"
    while True:
        store.set_state(task_id, "gating")
        await _autofix(task_id, wt, entry, emit)
        g = await asyncio.to_thread(gate.run_gate, wt, entry)
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
                             "reason": g.get("reason", ""), "feedback": feedback,
                             "fix-mode": mode})
        if await _resume_fix(task_id, wt, worker_model, emit, feedback, effort=effort,
                             fresh=fresh):
            # The worker changed nothing twice. That can be right (the gate was
            # flaky), so re-run the gate once before paging a person.
            store.set_state(task_id, "gating")
            await _autofix(task_id, wt, entry, emit)
            g = await asyncio.to_thread(gate.run_gate, wt, entry)
            emit("gate", g)
            if not g["passed"]:
                return _terminal(task_id, "needs_human", "worker-stalled")
            store.set_gate_steps(task_id, g.get("steps"), g.get("test_output"))
            break

    # --- one review pass ---------------------------------------------------
    if _review_capped(task_id):  # e.g. a human-resumed task that already used its rounds
        return _review_cap_stop(task_id, emit)
    store.set_state(task_id, "reviewing")
    # A review after an earlier `fix` review (agy/human-resumed round) is a re-review.
    prev = _last_review(task_id)
    head = await asyncio.to_thread(gate.head_sha, wt)
    if prev:
        rv_input = await asyncio.to_thread(gate.rereview_input, wt, entry, prev[0], prev[1])
        rv_model, rv_effort = rereview_model, rereview_effort
    else:
        rv_input = gate.review_diff(wt, entry)
        rv_model, rv_effort = review_model, review_effort
    r = await lane.review(rv_input, rv_model, emit, effort=rv_effort,
                          worktree=wt)
    store.add_cost(task_id, r.cost)
    if r.quota_reset_at:
        raise quota.QuotaWait(r.quota_reset_at, "review")
    emit("review", {"outcome": r.outcome, "verdict": r.verdict, "findings": r.findings,
                    "head": head, **({"pass": "re-review"} if prev else {})})
    if r.outcome == "reviewed":
        store.set_review(task_id, r.verdict, r.findings)
    if r.outcome == "review-error":
        return _terminal(task_id, "needs_human", "review-error")  # retryable — NOT a verdict
    if r.outcome == "reviewed" and _review_capped(task_id) and (
            r.verdict == "reject" or (r.verdict == "fix" and r.findings)):
        return _review_cap_stop(task_id, emit, r.findings)  # no fix round we can't re-review
    if r.verdict == "reject":
        return _terminal(task_id, "needs_human", "review-findings")
    if r.verdict == "fix" and r.findings:
        if fixes >= MAX_FIX:
            return _terminal(task_id, "needs_human", "review-findings")
        feedback = ("Address these review findings, then re-run tests and commit "
                   "(do not push):\n" + str(r.findings))
        emit("fix-trigger", {"source": "review", "findings": r.findings, "feedback": feedback,
                             "fix-mode": mode})
        if await _resume_fix(task_id, wt, worker_model, emit, feedback, effort=effort,
                             fresh=fresh):
            return _terminal(task_id, "needs_human", "worker-stalled")
        await _autofix(task_id, wt, entry, emit)
        g = await asyncio.to_thread(gate.run_gate, wt, entry)
        emit("gate", g)
        if not g["passed"]:
            return _terminal(task_id, "needs_human", "gate-red")
        store.set_gate_steps(task_id, g.get("steps"))
        # Re-review the fixed diff. Shipping on "the worker said it fixed it"
        # let two PRs land with their own reviewer's blocking finding still
        # open (2026-10). Only a fresh `approve` ships; anything else stops.
        if _review_capped(task_id):
            return _review_cap_stop(task_id, emit, r.findings)
        store.set_state(task_id, "reviewing")
        prev_findings = r.findings
        rv_input = await asyncio.to_thread(gate.rereview_input, wt, entry, head, prev_findings)
        head = await asyncio.to_thread(gate.head_sha, wt)
        r = await lane.review(rv_input, rereview_model, emit, effort=rereview_effort,
                              worktree=wt)
        store.add_cost(task_id, r.cost)
        if r.quota_reset_at:
            raise quota.QuotaWait(r.quota_reset_at, "re-review")
        emit("review", {"outcome": r.outcome, "verdict": r.verdict, "findings": r.findings,
                        "pass": "re-review", "head": head})
        if r.outcome == "reviewed":
            store.set_review(task_id, r.verdict, r.findings)
        if r.outcome == "review-error":
            return _terminal(task_id, "needs_human", "review-error")
        if r.verdict != "approve":
            if r.outcome == "reviewed" and _review_capped(task_id):
                return _review_cap_stop(task_id, emit, r.findings)
            return _terminal(task_id, "needs_human", "review-findings")

    return await _ship_tail(task_id, wt, emit)


async def _ship_tail(task_id: str, wt: Path, emit) -> str:
    t = store.get_task(task_id)
    # --- ship: control plane pushes + opens the PR (workers can't) ----------
    store.set_state(task_id, "shipping")
    import ship
    outcome, url = ship.ship(task_id)
    emit("ship", {"outcome": outcome, "pr_url": url})
    if outcome in ("pii-block", "pii-scan-error"):
        return _terminal(task_id, "needs_human", outcome)
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


async def _agy_continue(task_id: str, t: dict, emit) -> str:
    """agy-handoff pickup. Either a human pushed to coxd-agy/<id> (fetch it, gate, review,
    then ship from that branch or ask the human what to do with the findings), or the
    human chose "Let Claude fix" on a needs_choice card. Plain git only; agy is never run."""
    wt, repo = Path(t["worktree"]), t["repo"]
    cfg = roles.resolve(registry.load(repo) or {})
    emit("config", cfg)
    if agy_handoff.claude_fix_pending(task_id):
        return await _agy_claude_fix(task_id, t, wt, cfg, emit)
    store.set_state(task_id, "gating")
    sha = await asyncio.to_thread(agy_handoff.fetch_into_worktree, t)
    if not sha:
        return _terminal(task_id, "needs_human", "agy-fetch-error")
    emit("agy-pickup", {"sha": sha})
    entry = registry.get_or_scout(repo, wt)
    await _autofix(task_id, wt, entry, emit)
    g = await asyncio.to_thread(gate.run_gate, wt, entry)
    emit("gate", g)
    if not g["passed"]:
        if agy_handoff.rounds_used(task_id) >= agy_handoff.MAX_FIX_ROUNDS:
            return _terminal(task_id, "needs_human", "gate-red")
        plan = f"The gate failed at {g['failing']}:\n{g.get('reason', '')}\n"
        if not agy_handoff.send_enabled(repo):  # fixes always go to Claude, never back to agy
            agy_handoff.record_claude_round(task_id, "gate")
            return await _agy_claude_fix(
                task_id, t, wt, cfg, emit, source="gate",
                feedback="Fix this gate failure, then re-run tests and commit "
                         "(do not push):\n" + plan)
        agy_handoff.next_round(task_id, plan, "agy", "gate")
        return "waiting_agy"
    store.set_gate_steps(task_id, g.get("steps"), g.get("test_output"))
    if _review_capped(task_id):
        return _review_cap_stop(task_id, emit)
    store.set_state(task_id, "reviewing")
    prev = _last_review(task_id)
    head = await asyncio.to_thread(gate.head_sha, wt)
    if prev:  # a review after an earlier `fix` review: delta + previous findings
        rv_input = await asyncio.to_thread(gate.rereview_input, wt, entry, prev[0], prev[1])
        rv_model, rv_effort = cfg["rereview_model"], cfg["rereview_effort"]
    else:
        rv_input = gate.review_diff(wt, entry)
        rv_model, rv_effort = cfg["review_model"], cfg["review_effort"]
    r = await lane.review(rv_input, rv_model, emit,
                          effort=rv_effort, worktree=wt)
    store.add_cost(task_id, r.cost)
    if r.quota_reset_at:  # just wait for the reset (quota.park); never fall back
        raise quota.QuotaWait(r.quota_reset_at, "review")
    emit("review", {"outcome": r.outcome, "verdict": r.verdict, "findings": r.findings,
                    "head": head, **({"pass": "re-review"} if prev else {})})
    if r.outcome == "reviewed":
        store.set_review(task_id, r.verdict, r.findings)
    if r.outcome == "review-error":
        return _terminal(task_id, "needs_human", "review-error")
    if r.verdict == "approve":
        return await _ship_tail(task_id, wt, emit)
    if r.outcome == "reviewed" and _review_capped(task_id) and (
            r.verdict == "reject" or (r.verdict == "fix" and r.findings)):
        return _review_cap_stop(task_id, emit, r.findings)
    if r.verdict == "fix" and r.findings:
        if agy_handoff.rounds_used(task_id) >= agy_handoff.MAX_FIX_ROUNDS:
            return _terminal(task_id, "needs_human", "review-findings")
        if not agy_handoff.send_enabled(repo):  # no needs_choice stop: straight to Claude
            agy_handoff.record_claude_round(task_id, "review")
            return await _agy_claude_fix(task_id, t, wt, cfg, emit, source="review")
        store.set_state(task_id, "needs_choice", "review-findings")
        notify.notify_async("coxd · needs your choice (agy)",
                            f"{task_id}: review has findings — Send to agy or Let Claude fix",
                            priority="high")
        return "needs_choice"
    return _terminal(task_id, "needs_human", "review-findings")


async def _agy_claude_fix(task_id: str, t: dict, wt: Path, cfg: dict, emit,
                          source: str = "review", feedback: str | None = None) -> str:
    """"Let Claude fix": a fresh worker session on the worktree (already at the agy
    head), then the normal gate -> review -> ship tail. Pushes land on coxd-agy/<id>."""
    store.append_event(task_id, "agy-claude-fix-start", {})
    store.set_state(task_id, "fixing")
    sha = await asyncio.to_thread(agy_handoff.fetch_into_worktree, t)
    if not sha:
        return _terminal(task_id, "needs_human", "agy-fetch-error")
    if feedback is None:
        feedback = ("Address these review findings, then re-run tests and commit "
                    "(do not push):\n" + str(store.findings_of(store.get_task(task_id))))
    emit("fix-trigger", {"source": source, "feedback": feedback})
    wr = await lane.run_worker(wt, feedback, cfg["worker_model"], emit, resume=None,
                               effort=cfg["worker_effort"])
    store.set_session(task_id, wr.session_id)
    store.add_cost(task_id, wr.cost)
    store.set_summary(task_id, wr.summary)
    emit("agy-claude-fix-done", {"cost": wr.cost})
    if wr.quota_reset_at:
        raise quota.QuotaWait(wr.quota_reset_at, "fix")
    if wr.is_error or not wr.session_id:
        return _terminal(task_id, "needs_human", "worker-error")
    entry = registry.get_or_scout(t["repo"], wt)
    return await _gate_review_ship(task_id, wt, t["repo"], entry, cfg["worker_model"],
                                   cfg["review_model"], cfg["worker_effort"], emit,
                                   review_effort=cfg["review_effort"],
                                   rereview_model=cfg["rereview_model"],
                                   rereview_effort=cfg["rereview_effort"])


async def resume_task(task_id: str, note: str, worker_model: str | None = None,
                      review_model: str | None = None,
                      worker_effort: str | None = None, human_note: bool = False,
                      review_effort: str | None = None) -> str:
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

    cfg = roles.resolve(registry.load(repo), worker_model=worker_model,
                        review_model=review_model, worker_effort=worker_effort,
                        review_effort=review_effort)
    worker_model, review_model = cfg["worker_model"], cfg["review_model"]
    effort = cfg["worker_effort"]
    emit("config", cfg)
    emit("human-note", {"note": note})
    # Automatic resumes (crash-restart, usage-limit resume, one-shot auto-retry:
    # "continue from where you left off") are expected to change nothing when the
    # work is already committed, so no stall handling: straight to the gate.
    # A note a person/lead sent gets the fresh-session fallback (an exhausted
    # session can't silently drop it); "no change needed" is still a valid answer
    # there, so a second no-change is not terminal.
    await _resume_fix(task_id, wt, worker_model, emit, note, effort=effort,
                      stall_is_terminal=False, detect_stall=human_note)
    entry = registry.get_or_scout(repo, wt)
    return await _gate_review_ship(task_id, wt, repo, entry, worker_model,
                                   review_model, cfg["worker_effort"], emit,
                                   review_effort=cfg["review_effort"],
                                   rereview_model=cfg["rereview_model"],
                                   rereview_effort=cfg["rereview_effort"])


MAX_FRESH_PER_TASK = 2


def _snapshot(wt: Path) -> tuple[str, bool] | None:
    """(HEAD sha, has uncommitted changes), or None if git can't tell us."""
    try:
        head = sandbox.run_in_worktree(["git", "rev-parse", "HEAD"], wt)
        st = sandbox.run_in_worktree(["git", "status", "--porcelain"], wt)
    except Exception:
        return None
    if head.returncode != 0 or st.returncode != 0:
        return None
    return head.stdout.strip(), bool(st.stdout.strip())


def _stalled(before, after, wr) -> bool:
    """A fix round made no progress. The rule (all must hold):
      - the worker result is NOT an error (an error follows today's path),
      - no usage-limit cut-off (`quota_reset_at`; that parks via QuotaWait),
      - git state is readable before and after (unknown = not stalled),
      - HEAD is unchanged AND there are no uncommitted changes afterwards
        (and none were pre-existing, so we cannot mistake old dirt for work).
    A deliberate "no change needed" is only honoured where the caller says no
    change is a valid outcome (`stall_is_terminal=False`, i.e. a human note)."""
    if before is None or after is None or wr.quota_reset_at or wr.is_error:
        return False
    return before[0] == after[0] and not after[1] and not before[1]


def _fresh_prompt(task_id: str, wt: Path, feedback: str) -> str:
    t = store.get_task(task_id)
    reg = registry.load(t["repo"]) or {}
    brief = registry.data_guard_brief(reg, t["brief"])  # keep the synthetic-only rule
    return (f"Your earlier session ended without making any change, and its context is "
            f"gone. Task brief:\n{brief}\n\n"
            f"Feedback to address in this round:\n{feedback}\n\n"
            f"FIRST read `git log` and `git diff {gate._base_ref(wt)}...HEAD` to see what "
            "is already done, then make the fix, re-run the checks and commit. "
            "Do not push.")


MAX_BRIEF_DIFF_CHARS = 60_000


def _fix_brief(task_id: str, wt: Path, feedback: str) -> str:
    """Compact brief for a `fix_mode=fresh` round: intent + the branch's current diff +
    the feedback, so a new session needs no replay of the implement conversation."""
    t = store.get_task(task_id)
    reg = registry.load(t["repo"]) or {}
    intent = registry.data_guard_brief(reg, t["brief"])  # keep the synthetic-only rule
    base = gate._base_ref(wt)
    try:  # three-dot: changes on this branch since the merge-base, as gate.diff does
        stat = sandbox.run_in_worktree(["git", "diff", f"{base}...HEAD", "--stat"], wt).stdout
    except Exception:
        stat = ""
    try:
        full = gate.review_diff(wt, reg, budget=MAX_BRIEF_DIFF_CHARS) or ""
    except Exception:
        full = ""
    if len(full) > MAX_BRIEF_DIFF_CHARS:
        full = (full[:MAX_BRIEF_DIFF_CHARS] + f"\n[truncated at {MAX_BRIEF_DIFF_CHARS} chars; "
                f"run `git diff {base}...HEAD` for the rest]")
    return (f"Task brief (the original intent):\n{intent}\n\n"
            "Work so far: this branch already contains these changes "
            f"(vs {base}).\n\nDiff stat:\n{stat.strip() or '(unavailable)'}\n\n"
            f"Full diff:\n{full.strip() or '(unavailable)'}\n\n"
            f"Feedback to address in this round:\n{feedback}\n\n"
            "Fix minimally, re-run the checks and commit. Do not push.")


async def _resume_fix(task_id: str, wt: Path, model: str, emit, feedback: str,
                      effort: str = "medium", stall_is_terminal: bool = True,
                      detect_stall: bool = True, fresh: bool = False) -> bool:
    """Run one fix round on the resumed session. Returns True when the worker is
    stalled (caller decides: gate path re-runs the gate once, review path stops
    with needs_human/worker-stalled).

    A session that exhausts its task budget replies "out of budget", edits nothing
    and exits normally. If the round was a stall (see `_stalled`), start ONE fresh
    session (resume=None) in the same worktree (one per round, MAX_FRESH_PER_TASK per
    task). If that also makes no change (or the cap is spent) the round is stalled,
    unless `stall_is_terminal=False` (human note: "no change needed" is valid, so
    return False and follow the normal path). `detect_stall=False` skips stall
    handling entirely (automatic resumes).

    `fresh=True` (per-repo `fix_mode: fresh`) starts a NEW session with a compact brief
    (`_fix_brief`) instead of resuming the full implement history."""
    store.set_state(task_id, "fixing")
    if fresh:
        sid, prompt = None, _fix_brief(task_id, wt, feedback)
    else:
        sid, prompt = store.get_task(task_id)["session_id"], feedback  # resume the SAME session
    before = _snapshot(wt)
    wr = await lane.run_worker(wt, prompt, model, emit, resume=sid, effort=effort)
    store.add_cost(task_id, wr.cost)
    store.set_summary(task_id, wr.summary)
    if wr.session_id:
        store.set_session(task_id, wr.session_id)
    if wr.quota_reset_at:
        raise quota.QuotaWait(wr.quota_reset_at, "fix")
    if not detect_stall or not _stalled(before, _snapshot(wt), wr):
        return False
    used = sum(1 for e in store.events(task_id) if e["kind"] == "fresh-session")
    if used >= MAX_FRESH_PER_TASK:
        emit("worker-stalled", {"reason": "fresh-session cap reached"})
        return stall_is_terminal
    emit("fresh-session", {"reason": "fix round made no change (HEAD unchanged, "
                                     "tree clean); session likely out of budget"})
    before = _snapshot(wt)
    wr = await lane.run_worker(wt, _fresh_prompt(task_id, wt, feedback), model, emit,
                               resume=None, effort=effort)
    store.add_cost(task_id, wr.cost)
    store.set_summary(task_id, wr.summary)
    if wr.session_id:
        store.set_session(task_id, wr.session_id)
    if wr.quota_reset_at:
        raise quota.QuotaWait(wr.quota_reset_at, "fix")
    if _stalled(before, _snapshot(wt), wr):
        emit("worker-stalled", {"reason": "fresh session also made no change"})
        return stall_is_terminal
    return False


def _terminal(task_id: str, state: str, reason: str | None) -> str:
    store.set_state(task_id, state, reason)
    if state in ("pr_ready", "needs_human"):  # the AFK ping
        t = store.get_task(task_id)
        if state == "pr_ready":
            if t and t.get("lane") != AGY_LANE:
                notify.notify_async("coxd · ready to merge", f"{task_id} ({t['repo']})")
        else:
            import triage
            notify.notify_async(f"coxd · needs you ({reason})",
                                f"{task_id} ({t['repo']})\n{triage.next_step_line(t)}",
                                priority="high")
    return state

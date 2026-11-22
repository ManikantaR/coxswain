"""The board — a STATELESS reader over coxd's SQLite store (DESIGN-V35).

Unlike the old dashboard (which tailed log files and could show fiction), this
reads the one truthful store. If it dies, coxd keeps running; restart it and the
db is the truth. Starlette + SSE (deps already pulled in by the Agent SDK).

Run from coxd/:  .venv/bin/python board.py   (COXD_HOME selects the store)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import re
import secrets
import socket
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import agy_handoff
import factory
import intake
import pii_health
import registry
import scorecard
import spend
import stepspend
import store
import timeline

# Every dispatch brief in this project includes a line like
# "GitHub issue: https://github.com/<owner>/<repo>/issues/<n>" by convention —
# extract it so the board can link straight to the issue, not just show the title.
_ISSUE_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/\d+")
import uvicorn
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

STAGES = ["implement", "gate", "review", "merge"]
_STATE_STAGE = {"queued": 0, "provisioning": 0, "working": 0, "gating": 1,
                "fixing": 1, "reviewing": 2, "shipping": 3, "ci-watching": 3,
                "pr_ready": 3, "landed": 3, "waiting_quota": 1,
                "planning": 0, "waiting_agy": 0, "needs_choice": 2}
_ACTIVE = {"planning", "provisioning", "working", "gating", "fixing", "reviewing", "shipping",
          "ci-watching"}
_NEEDS_YOU = {"pr_ready", "needs_human", "waiting_agy", "needs_choice"}
# Terminal, resolved states eligible for silent age-based archiving — never
# pr_ready/needs_human, those always need a human look regardless of age.
_ARCHIVABLE = store.ARCHIVABLE
# Manual (explicit button/API) archiving is allowed from pr_ready/needs_human
# too — a human has already decided the task is dead (e.g. superseded, or its
# underlying issue closed via another path), so hiding it is a deliberate
# choice, not silent cleanup.
_MANUAL_ARCHIVABLE = _ARCHIVABLE | {"pr_ready", "needs_human"}
_ARCHIVE_DAYS = store.ARCHIVE_DAYS


# Maps a needs_human `reason` to the stage it actually failed at — previously
# every needs_human task showed at "review" regardless of where it really failed
# (a worker-error during implement looked identical to a real review rejection).
_REASON_STAGE = {
    "worker-error": 0, "coxd-error": 0,
    "gate-red": 1, "worker-stalled": 1,
    "review-error": 2, "review-findings": 2,
    "push-rejected": 3, "pr-error": 3,
    "pii-block": 3, "pii-scan-error": 3,
}


def _reason_stage(reason: str | None) -> int:
    if not reason:
        return 2  # unknown reason — review is the least-wrong default
    if reason.startswith("ci-red"):
        return 3
    return _REASON_STAGE.get(reason, 2)


def _stage(state: str, reason: str | None = None) -> dict:
    if state == "landed":
        return {"i": 3, "status": "done"}
    if state == "needs_human":
        return {"i": _reason_stage(reason), "status": "error"}
    return {"i": _STATE_STAGE.get(state, 0), "status": "active"}


AGY_STAGES = ["plan", "agy", "gate", "review", "pr"]
_AGY_SHIFT = {0: 1, 1: 2, 2: 3, 3: 4}  # claude-lane stage index -> agy-lane index


def _has_current_plan(evs: list[dict]) -> bool:
    """True when a `plan` event exists after the last agy `fix-round` (or any `plan`
    when there is none). Claude fix rounds (via != agy) write no new plan, so they
    don't reset it."""
    for e in reversed(evs):
        kind = e.get("kind")
        if kind == "plan":
            return True
        if kind == "fix-round":
            d = e.get("data") if isinstance(e.get("data"), dict) else {}
            if d.get("via") == "agy":
                return False
    return False


def _agy_stage(state: str, reason: str | None, has_plan: bool = False) -> dict:
    if state == "planning":
        return {"i": 0, "status": "active"}
    if state == "waiting_agy":
        return {"i": 1, "status": "active"}
    st = _stage(state, reason)
    if state in ("queued", "provisioning", "working"):  # picked up, not yet gating
        i = 1 if has_plan else 0
        return {"i": i, "status": "active" if st["status"] != "error" else "error"}
    return {"i": _AGY_SHIFT[st["i"]], "status": st["status"]}


def _agy_info(evs: list[dict]) -> dict:
    """Card facts for an agy-handoff task, derived from its events (read-only)."""
    cost_split = {"plan": 0.0, "implement": 0.0, "fix": 0.0, "review": 0.0}
    tokens = None
    models: dict[str, str] = {}
    pickup, waiting_since = None, None
    norm_evs: list[dict] = []
    for e in evs:
        d = e.get("data") if isinstance(e.get("data"), dict) else {}
        kind = e.get("kind")
        if kind == "state":
            st = d.get("state")
            waiting_since = e["ts"] if st == "waiting_agy" else None
            # stepspend.phased_results maps provisioning/working -> implement;
            # normalize waiting_agy so any result there counts as implement.
            norm_evs.append({"kind": "state", "data": {"state": "working"}}
                            if st == "waiting_agy" else e)
            continue
        if kind == "agy-pickup":
            pickup = {"sha": str(d.get("sha") or "")[:7], "ts": e["ts"]}
        if isinstance(d.get("agy_tokens"), int) and not isinstance(d.get("agy_tokens"), bool):
            tokens = d["agy_tokens"]
        norm_evs.append(e)
    for phase, e in stepspend.phased_results(norm_evs):
        d = e["data"]
        c = stepspend._num(d.get("cost"))
        if phase in cost_split:
            cost_split[phase] += c
        m = d.get("model")
        if isinstance(m, str) and m:
            if phase == "plan":
                models["0"] = stepspend.short_name(m)
            elif phase == "review":
                models["3"] = stepspend.short_name(m)
    return {
        "cost_split": cost_split,
        "claude_total": sum(cost_split.values()),
        "agy_tokens": tokens,
        "models": models,
        "pickup": pickup,
        "waiting_since": waiting_since,
    }


_STATE_SENTENCES: dict[str, str] = {
    "queued": "Queued",
    "provisioning": "Setting up workspace",
    "planning": "Writing plan",
    "waiting_agy": "Waiting for you to run agy",
    "working": "Implementing changes",
    "gating": "Running checks",
    "fixing": "Fixing issues",
    "reviewing": "Reviewing changes",
    "needs_choice": "Waiting for your choice",
    "shipping": "Opening pull request",
    "ci-watching": "Watching CI checks",
    "waiting_quota": "Paused: Claude usage limit",
    "pr_ready": "Ready to merge",
    "landed": "Merged",
    "needs_human": "Needs your attention",
    "failed": "Failed",
    "running": "Running",
}

_STATUS_SENTENCES: dict[str, str] = {
    "agy-pickup": "Checking your push",
    "agy-head": "Checking your push",
    "agy-applied": "Loaded your push",
    "agy-claude-fix": "Queued for Claude to fix",
    "agy-claude-fix-done": "Claude finished fixing",
    "plan": "Plan written",
    "ci-watch": "Watching CI checks",
    "provision": "Workspace ready",
    "human-note": "Resuming with your note",
    "resume-request": "Resume requested",
    "worker-stalled": "Worker stalled without changes",
    "fresh-session": "Retrying in a fresh session",
    "deployed": "Deployed",
    "deploy-start": "Deploying",
    "greenlight": "Greenlight check finished",
    "pii-block": "Blocked by privacy check",
    "streak": "Merge streak updated",
}


def _fmt_hhmm(ts) -> str | None:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
        return None
    tz_name = os.environ.get("COXD_TZ", "America/New_York")
    try:
        z = ZoneInfo(tz_name)
    except Exception:
        z = ZoneInfo("UTC")
    try:
        return datetime.fromtimestamp(ts, z).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return None


def status_line(kind, data, state=None, *, fix_round: int | None = None) -> str:
    """Map an event kind + payload to a short plain-language sentence, or ''."""
    # Silent/internal kinds (rate_limit, created, result, config, tool, say) and
    # anything unknown or malformed return "".
    if not isinstance(kind, str) or not isinstance(data, dict):
        return ""
    if kind in _STATUS_SENTENCES:
        return _STATUS_SENTENCES[kind]
    if kind == "state":
        s = data.get("state") or state
        return _STATE_SENTENCES.get(s, "") if isinstance(s, str) else ""
    if kind == "agy-claude-fix-start":
        rnd = (fix_round if isinstance(fix_round, int) and not isinstance(fix_round, bool)
               and fix_round > 0 else data.get("round"))
        n = rnd if isinstance(rnd, int) and not isinstance(rnd, bool) and rnd > 0 else 1
        return f"Claude fixing review findings (round {n})"
    if kind == "waiting-quota":
        hhmm = _fmt_hhmm(data.get("resume_at"))
        return f"Paused: Claude usage limit, resumes {hhmm}" if hhmm else "Paused: Claude usage limit"
    if kind == "review":
        if data.get("outcome") == "review-error":
            return "Review error"
        verdict = data.get("verdict")
        if verdict == "approve":
            return "Review approved"
        if verdict == "fix":
            return "Review requested fixes"
        if verdict == "reject":
            return "Review rejected"
        return "Review finished"
    if kind == "ci-triage":
        outcome = data.get("outcome")
        if outcome in ("green", "flaky-cleared"):
            return "CI checks passed"
        if outcome == "needs_human":
            return "CI checks failed"
        return "CI checks finished"
    if kind == "fix-round":
        via = data.get("via")
        if via == "claude":
            return "Starting Claude fix round"
        if via == "agy":
            return "Sending fix round to agy"
        return "Starting fix round"
    if kind == "agy-error":
        return "Failed to push agy branch" if data.get("stage") == "push" else "Agy handoff error"
    if kind == "agy-status":
        action = data.get("action")
        if isinstance(action, str) and action.strip() and "{" not in action:
            return f"agy: {action.strip()}"
        return "Running agy"
    if kind == "gate":
        passed = data.get("passed")
        if passed is True:
            return "Checks passed"
        if passed is False:
            failing = data.get("failing")
            return f"Check failed: {failing}" if isinstance(failing, str) and failing else "Checks failed"
        return "Checks finished"
    if kind == "ship":
        outcome = data.get("outcome")
        if outcome == "pr":
            return "Pull request opened"
        if outcome == "local":
            return "Changes ready on local branch"
        if outcome in ("pii-block", "pii-scan-error"):
            return "Blocked by privacy check"
        if outcome in ("push-error", "pr-error"):
            return "Failed to open pull request"
        return "Shipping finished"
    if kind == "fix-trigger":
        src = data.get("source")
        if src == "gate":
            return "Fixing failed check"
        if src == "review":
            return "Fixing review findings"
        return "Starting fix"
    if kind == "error":
        return "Stopped by user" if data.get("error") == "stopped by user" else "Task failed"
    if kind == "info":
        note = str(data.get("note") or "")
        if "usage window reset" in note:
            return "Usage window reset — resuming"
        if "auto-resume" in note:
            return "Auto-resuming after restart"
        if "auto-retry" in note:
            return "Auto-retrying after restart"
        return ""
    return ""


def _last_status(evs: list[dict], state: str | None = None) -> str:
    """Most recent non-empty plain-language status across `evs`, or ''."""
    for idx in range(len(evs) - 1, -1, -1):
        e = evs[idx]
        kind = e.get("kind")
        fix_round = (sum(1 for prev in evs[:idx] if prev.get("kind") == "fix-round")
                     if kind == "agy-claude-fix-start" else None)
        line = status_line(kind, e.get("data"), state=state, fix_round=fix_round)
        if line:
            return line
    return ""


PLAN_CLAUDE_STAGES = ["plan", "implement", "gate", "review", "merge"]


def _plan_claude_stage(state: str, reason: str | None) -> dict:
    """Stage for a Claude-lane card that ran plan_first: plan is step 0, the rest shift +1.
    (Not _agy_stage: that treats waiting_agy/queued as agy-only states.)"""
    if state == "planning":
        return {"i": 0, "status": "active"}
    st = _stage(state, reason)
    return {"i": st["i"] + 1, "status": st["status"]}


def _plan_info(evs: list[dict], state: str) -> dict | None:
    """Plan-step facts for a Claude-lane task, or None when no plan ran (read-only).

    Plan ran = a `plan` event exists or the task is in `planning` now."""
    phase, plan_ev, model, run_cost = None, None, None, 0.0
    cfg_model = None
    for e in evs:
        d = e.get("data") if isinstance(e.get("data"), dict) else {}
        if e["kind"] == "state":
            phase = d.get("state")
        elif e["kind"] == "config":
            if isinstance(d.get("planner_model"), str) and d["planner_model"]:
                cfg_model = d["planner_model"]
        elif e["kind"] == "plan":
            plan_ev = d
        elif e["kind"] == "result" and phase == "planning":
            run_cost += stepspend._num(d.get("cost"))
            if isinstance(d.get("model"), str) and d["model"]:
                model = d["model"]
    if plan_ev is None and state != "planning":
        return None
    md = plan_ev.get("plan_md") if plan_ev else None
    cost = stepspend._num(plan_ev.get("cost")) if plan_ev and "cost" in plan_ev else run_cost
    m = model or cfg_model
    return {"plan_md": md if isinstance(md, str) and md else None, "plan_cost": cost,
            "plan_model": stepspend.short_name(m) if m else None}


def _plan_claude_models(evs: list[dict], plan_model: str | None) -> dict[str, str]:
    """Stage models for the 5-step Claude card: implement 1, review 3, plan 0.
    Plan-phase results are excluded so the planner never shows as the implementer."""
    out: dict[str, str] = {}
    for phase, e in stepspend.phased_results(evs):
        m = e["data"].get("model")
        if phase != "plan" and isinstance(m, str) and m:
            out["3" if phase == "review" else "1"] = stepspend.short_name(m)
    if plan_model:
        out["0"] = plan_model
    return out


_is_archived = store.is_archived


_DEPLOY_CACHE: dict[str, tuple[float, bool]] = {}
_DEPLOY_CACHE_TTL_S = 30.0


def _repo_has_deploy(repo: str) -> bool:
    """Does this repo have a deploy command configured? Cached briefly so a board
    full of tasks doesn't re-read the same registry file N times, but expires so a
    newly enabled deploy shows its button without restarting coxd."""
    hit = _DEPLOY_CACHE.get(repo)
    if hit is None or time.time() - hit[0] > _DEPLOY_CACHE_TTL_S:
        has = bool(((registry.load(repo) or {}).get("deploy") or {}).get("command"))
        _DEPLOY_CACHE[repo] = (time.time(), has)
    return _DEPLOY_CACHE[repo][1]


_SEV_CHIP = {"high": "critical", "med": "major", "low": "minor"}


def _last_claude_fix_cost(evs: list[dict]) -> float | None:
    """What the last Claude fix round cost (about), from its step spend; None if never."""
    for e in reversed(evs):
        if e["kind"] == "agy-claude-fix-done" and (e.get("data") or {}).get("cost") is not None:
            return float(e["data"]["cost"])
    return None


def _choice_payload(t: dict, evs: list[dict]) -> dict:
    used = sum(1 for e in evs if e["kind"] == "fix-round")
    capped = used >= agy_handoff.MAX_FIX_ROUNDS
    findings = [{"severity": _SEV_CHIP.get(str(f.get("severity")), "minor"),
                 "summary": str(f.get("summary") or ""), "file": f.get("file"),
                 "line": f.get("line")}
                for f in store.findings_of(t)]
    order = ("critical", "major", "minor")
    findings.sort(key=lambda f: order.index(f["severity"]))
    return {"findings": findings, "round": used + 1, "capped": capped,
            "cap_reason": f"fix-round cap ({agy_handoff.MAX_FIX_ROUNDS}) reached" if capped else None,
            "claude_cost": _last_claude_fix_cost(evs),
            "send_enabled": agy_handoff.send_enabled(t["repo"])}


def _agy_status_view(t: dict, info: dict | None) -> dict | None:
    """Latest script-reported status, only while waiting_agy and from this round."""
    st = _AGY_STATUS.get(t["id"])
    if not info or t["state"] != "waiting_agy" or not st:
        return None
    if st["ts"] < (info.get("waiting_since") or 0):
        return None  # left over from an earlier round
    return st


def _tasks_payload(show_archived: bool = False) -> dict:
    rows = []
    archived_count = 0
    now = time.time()
    all_tasks = store.list_tasks()
    all_evs: list[list[dict]] = []
    all_lanes: list[str] = []
    evs_by_id: dict[str, list[dict]] = {}
    levels: dict[str, str] = {}  # one registry read per repo per poll
    budgets: dict[str, float] = {}  # one registry read per repo per poll
    for t in all_tasks:
        evs = store.events(t["id"])
        all_evs.append(evs)  # month spend + scorecard count every task, archived or not
        all_lanes.append(stepspend.normalize_lane(t.get("lane")))
        evs_by_id[t["id"]] = evs
        if _is_archived(t):
            archived_count += 1
            if not show_archived:
                continue
        is_agy = t.get("lane") == "agy-handoff"
        info = _agy_info(evs) if is_agy else None
        pinfo = None if is_agy else _plan_info(evs, t["state"])
        issue_match = _ISSUE_URL_RE.search(t.get("brief") or "")
        if t["repo"] not in budgets:
            budgets[t["repo"]] = registry.task_budget(t["repo"])
        budget = budgets[t["repo"]]
        rows.append({
            "id": t["id"], "repo": t["repo"], "state": t["state"], "reason": t["reason"],
            "cost": t["cost"], "budget": budget,
            "over_budget": scorecard.over_budget(t["cost"], budget),
            "pr_url": t["pr_url"],
            "stage": (_agy_stage(t["state"], t["reason"], _has_current_plan(evs)) if is_agy
                      else (_plan_claude_stage if pinfo else _stage)(t["state"], t["reason"])),
            "lane": "agy" if is_agy else "claude",
            "stages": AGY_STAGES if is_agy else PLAN_CLAUDE_STAGES if pinfo else None,
            "plan_md": t.get("plan_md") if is_agy else pinfo["plan_md"] if pinfo else None,
            "plan_cost": pinfo["plan_cost"] if pinfo else None,
            "claude_split": info["cost_split"] if info else None,
            "claude_total": info["claude_total"] if info else None,
            "agy_tokens": info["agy_tokens"] if info else None,
            "agy_pickup": info["pickup"] if info else None,
            "waiting_since": info["waiting_since"] if info else None,
            "active": t["state"] in _ACTIVE, "needs_you": t["state"] in _NEEDS_YOU,
            "issue_url": issue_match.group(0) if issue_match else None,
            "archivable": t["state"] in _MANUAL_ARCHIVABLE,
            "archived": bool(t.get("archived_at")),
            "resumable": bool(t.get("session_id")),
            "agy_cmd": f"agy-next {t['id']}" if t["state"] == "waiting_agy" else None,
            "agy_round": (sum(1 for e in evs if e["kind"] == "plan")
                          if is_agy else None),
            "agy_status": _agy_status_view(t, info),
            "choice": _choice_payload(t, evs) if t["state"] == "needs_choice" else None,
            "quota_wait_at": agy_handoff.quota_wait_at(t) if t.get("lane") == "agy-handoff" else None,
            # Deploy button shows on a landed task whose repo has a deploy command.
            "approve_auto": levels.setdefault(
                t["repo"], registry.merge_level(t["repo"])) == "proposed",
            "deployable": t["state"] == "landed" and _repo_has_deploy(t["repo"]),
            "last": _last_status(evs, t["state"]),
            "timing": timeline.derive(t, evs, now),
            "models": (info["models"] if is_agy else
                       _plan_claude_models(evs, pinfo["plan_model"]) if pinfo else
                       {str(k): v for k, v in stepspend.stage_models(evs).items()}),
            "resume_at": t.get("resume_at"),
            "_updated": t["updated"] or t["created"] or 0,
        })
    # needs-you first, then active, then most-recently-updated first within each
    # tier — previously fell back to alphabetical-by-id, which isn't chronological
    # (task ids are name-slug prefixed, only suffixed with a timestamp).
    rows.sort(key=lambda r: (not r["needs_you"], not r["active"], -r["_updated"]))
    for r in rows:
        del r["_updated"]
    tz = os.environ.get("COXD_TZ", "America/New_York")
    spend_now = spend.status()  # cost_ledger: the one authoritative month total
    return {"tasks": rows, "stages": STAGES,
            "tz": tz,
            "needs_you": sum(r["needs_you"] for r in rows),
            "active": sum(r["active"] for r in rows),
            "archived": archived_count, "archive_days": _ARCHIVE_DAYS,
            "model_spend": stepspend.month_spend(all_evs, tz, now, ledger_total=spend_now["mtd"]),
            "phase_bars": {
                "month": stepspend.phase_spend(all_evs, stepspend.month_start(now, tz), lanes=all_lanes),
                "all_time": stepspend.phase_spend(all_evs, 0.0, lanes=all_lanes),
            },
            "scorecard": scorecard.build(all_tasks, evs_by_id, store.streaks()),
            "cost_trend": scorecard.weekly_cost_per_landed(all_tasks, evs_by_id, now, tz, weeks=8),
            "factory": factory.status(), "intake": intake.status(), "pii": pii_health.pill(), "spend": spend_now}


async def index(_: Request) -> HTMLResponse:
    return HTMLResponse(_HTML)


async def api_tasks(req: Request) -> JSONResponse:
    return JSONResponse(_tasks_payload(show_archived=req.query_params.get("all") == "1"))


async def api_events(req: Request) -> JSONResponse:
    tid = req.path_params["tid"]
    return JSONResponse({"events": [
        {"kind": e["kind"], "data": e["data"], "ts": e["ts"]}
        for e in store.events(tid)][-60:]})


async def api_task_plan(req: Request) -> JSONResponse:
    """Read-only: the Claude plan for an agy-handoff task (the human's own script reads it)."""
    tid = req.path_params["tid"]
    t = store.get_task(tid)
    if t is None or t.get("lane") != "agy-handoff":
        return JSONResponse({"error": "no such agy-handoff task"}, status_code=404)
    evs = [e for e in store.events(tid) if e["kind"] == "plan"]
    return JSONResponse({"task_id": tid, "repo": t["repo"],
                         "branch": f"coxd-agy/{tid}", "base_sha": t.get("base_sha"),
                         "round": len(evs), "plan_md": t.get("plan_md")})


async def api_agy_queue(_: Request) -> JSONResponse:
    """Read-only: ordered agy-handoff queue with file-overlap blocking status."""
    tasks, events, open_pr_files, issue_labels = await asyncio.to_thread(
        agy_handoff.queue_inputs)
    rows = agy_handoff.queue(tasks, events, open_pr_files, issue_labels)
    n_ready = sum(1 for r in rows if r["status"] == "ready")
    n_blocked = len(rows) - n_ready
    return JSONResponse({"queue": rows, "ready": n_ready, "blocked": n_blocked})


# Live agy progress, posted by the human's own `agy-next` script. Display only: held in
# memory (lost on restart — the next post refills it), never used for a state transition.
_AGY_STATUS: dict[str, dict] = {}
_AGY_EVENT_AT: dict[str, float] = {}
AGY_EVENT_EVERY_S = 60
_AGY_STATUS_MAX_BODY = 4096
_AGY_ACTION_MAX = 120
_AGY_INT_MAX = {"step": 1_000_000, "elapsed_s": 7 * 86400, "tokens": 10_000_000_000}


def _agy_status_clean(body) -> tuple[dict | None, str]:
    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    out: dict = {}
    for k, hi in _AGY_INT_MAX.items():
        v = body.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= hi:
            return None, f"{k} must be an integer in 0..{hi}"
        out[k] = v
    a = body.get("action")
    if not isinstance(a, str) or not a.strip() or not a.isprintable():
        return None, "action must be a non-empty printable string"
    out["action"] = a.strip()[:_AGY_ACTION_MAX]
    return out, ""


async def api_task_agy_status(req: Request) -> JSONResponse:
    """The human's agy-next script reports one short progress line for the card."""
    tid = req.path_params["tid"]
    t = store.get_task(tid)
    if t is None or t.get("lane") != "agy-handoff":
        return JSONResponse({"error": "no such agy-handoff task"}, status_code=404)
    if t["state"] != "waiting_agy":
        return JSONResponse({"error": f"task is '{t['state']}', not waiting_agy"}, status_code=400)
    if req.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return JSONResponse({"error": "content-type must be application/json"}, status_code=415)
    raw = await req.body()
    if len(raw) > _AGY_STATUS_MAX_BODY:
        return JSONResponse({"error": "body too large"}, status_code=413)
    try:
        body = json.loads(raw)
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    st, err = _agy_status_clean(body)
    if st is None:
        return JSONResponse({"error": err}, status_code=400)
    now = time.time()
    st["ts"] = now
    _AGY_STATUS[tid] = st
    if now - _AGY_EVENT_AT.get(tid, 0.0) >= AGY_EVENT_EVERY_S:
        _AGY_EVENT_AT[tid] = now
        store.append_event(tid, "agy-status", {k: st[k] for k in ("step", "action", "elapsed_s", "tokens")})
    return JSONResponse({"ok": True})


async def api_task_choice(req: Request) -> JSONResponse:
    """needs_choice buttons: {"choice": "send"} (Send to agy) | {"choice": "claude"}
    (Let Claude fix). The next agy round / worker fix is queued; nothing runs here."""
    tid = req.path_params["tid"]
    try:
        body = await req.json()
    except ValueError:
        body = {}
    ok, err = agy_handoff.choose(tid, str((body or {}).get("choice") or ""))
    if not ok:
        code = 404 if err.startswith("no such") else 400
        return JSONResponse({"error": err}, status_code=code)
    return JSONResponse({"ok": True})


async def api_task_retry(req: Request) -> JSONResponse:
    """Re-queue a needs_human task from scratch — same as the manual `UPDATE tasks
    SET state='queued'` this board grew out of doing by hand. Only sane before any
    commit landed in the worktree (e.g. worker-error/auth-error); for a task with
    real committed work already, prefer /resume so the existing session's context
    isn't thrown away."""
    tid = req.path_params["tid"]
    t = store.get_task(tid)
    if not t:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    if t["state"] != "needs_human":
        return JSONResponse({"error": f"cannot retry from state '{t['state']}'"}, status_code=400)
    store.set_state(tid, "queued", None)
    return JSONResponse({"ok": True})


async def api_task_stop(req: Request) -> JSONResponse:
    """Cancel an in-flight task in place — the missing option before today (see
    2026-07-27: a mistaken /retry on a task that already had a green PR raced
    to completion and opened a duplicate PR, with no way to interrupt it short
    of merging the original PR first). Only valid while actually running;
    lands at needs_human/stopped-by-user, same unstick path (/resume) as any
    other needs_human task — whatever was already committed is not discarded."""
    tid = req.path_params["tid"]
    t = store.get_task(tid)
    if not t:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    if t["state"] not in _ACTIVE:
        return JSONResponse({"error": f"cannot stop from state '{t['state']}'"}, status_code=400)
    import supervisor
    if not supervisor.cancel(tid):
        return JSONResponse({"error": "task isn't actually running (already finishing?)"},
                            status_code=409)
    return JSONResponse({"ok": True})


async def api_task_resume(req: Request) -> JSONResponse:
    """Resume the SAME worker session with a human-provided note, then re-run the
    shared gate->review->ship tail. For a needs_human task that already has real
    committed work — a targeted unstick, not a from-scratch retry."""
    tid = req.path_params["tid"]
    body = await req.json()
    note = str(body.get("note") or "").strip()
    if not note:
        return JSONResponse({"error": "note is required"}, status_code=400)
    t = store.get_task(tid)
    if not t:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    # pr_ready too: the lead's own review often finds something after coxd's reviewer
    # approved — the fix goes back to the same session and onto the same open PR.
    if t["state"] not in ("needs_human", "pr_ready"):
        return JSONResponse({"error": f"cannot resume from state '{t['state']}'"}, status_code=400)
    if not t["session_id"]:
        return JSONResponse({"error": "no prior session — use retry instead"}, status_code=400)
    # Queue it for the supervisor instead of running it here: that way it respects
    # the concurrency cap, the factory switch and the usage-limit wait, Stop can cancel
    # it, and a crash lands at needs_human instead of leaving it stuck "active".
    store.append_event(tid, "resume-request", {"note": note})
    store.set_state(tid, "queued", HUMAN_RESUME)
    return JSONResponse({"ok": True, "queued": True})


async def api_task_deploy(req: Request) -> JSONResponse:
    """Human-gated deploy: build + recreate this task's repo on the NAS at current
    main. coxd holds the Docker socket but only acts on this tap. Runs in a
    background thread; progress lands in the task's event feed + an AFK ping."""
    import landed
    res = landed.deploy_task_async(req.path_params["tid"])
    return JSONResponse(res, status_code=400 if res.get("error") else 200)


async def api_repo_approve_auto(req: Request) -> JSONResponse:
    """The ONLY path to merge_level=auto: the owner taps 'Approve auto-merge' on a
    repo whose streak earned a proposal."""
    repo = req.path_params["repo"]
    if not store.approve_auto_merge(repo):
        return JSONResponse({"error": "no pending auto-merge proposal for this repo"},
                            status_code=400)
    return JSONResponse({"ok": True, "merge_level": "auto"})


async def api_task_archive(req: Request) -> JSONResponse:
    tid = req.path_params["tid"]
    t = store.get_task(tid)
    if not t:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    if t["state"] not in _MANUAL_ARCHIVABLE:
        return JSONResponse(
            {"error": f"cannot archive from state '{t['state']}'"},
            status_code=400,
        )
    store.archive_task(tid)
    return JSONResponse({"ok": True})


async def api_task_unarchive(req: Request) -> JSONResponse:
    tid = req.path_params["tid"]
    t = store.get_task(tid)
    if not t:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    store.unarchive_task(tid)
    return JSONResponse({"ok": True})


def _factory_payload() -> dict:
    return {
        **factory.status(),
        "host": os.environ.get("COXD_HOST_LABEL") or socket.gethostname(),
        "role": os.environ.get("COXD_ROLE") or "primary",
    }


async def api_factory(req: Request) -> JSONResponse:
    """GET: is this instance's factory ON or OFF. POST {"paused": bool}: flip it.
    OFF starts nothing new; running tasks finish; the queue waits (see factory.py)."""
    if req.method == "GET":
        return JSONResponse(_factory_payload())
    try:
        body = await req.json()
    except ValueError:
        return JSONResponse({"error": "JSON body required"}, status_code=400)
    if not isinstance(body.get("paused"), bool):
        return JSONResponse({"error": "paused must be true or false"}, status_code=400)
    return JSONResponse(factory.set_paused(body["paused"]))


async def api_restart(_: Request) -> JSONResponse:
    """Restart THIS supervisor process in place (os.execv — same PID, fresh code
    and fresh credentials from disk). Refuses while any task is active: an
    in-flight SDK session would be orphaned mid-call, exactly what happened
    tonight requiring a manual worktree inspection + re-queue afterward."""
    payload = _tasks_payload()
    if payload["active"] > 0:
        return JSONResponse(
            {"error": f"{payload['active']} task(s) still active — wait for them "
                      "to finish or they'll be orphaned mid-session"}, status_code=409)
    import os
    import sys
    asyncio.get_event_loop().call_later(
        0.3, lambda: os.execv(sys.executable, [sys.executable] + sys.argv))
    return JSONResponse({"ok": True, "restarting": True})


async def sse(req: Request) -> EventSourceResponse:
    show_archived = req.query_params.get("all") == "1"

    async def gen():
        while True:
            yield {"data": json.dumps(_tasks_payload(show_archived=show_archived))}
            await asyncio.sleep(2)
    return EventSourceResponse(gen())


async def api_repos(_: Request) -> JSONResponse:
    import repos
    return JSONResponse({"repos": repos.list_repos()})


async def api_issues(req: Request) -> JSONResponse:
    import repos
    out = repos.list_issues(req.query_params.get("repo", ""))
    return JSONResponse(out, status_code=400 if out.get("error") else 200)


async def api_dispatch(req: Request) -> JSONResponse:
    import dispatch
    import repos
    body = await req.json()
    repo = str(body.get("repo") or "").strip()
    if not repo:
        return JSONResponse({"error": "pick a repo"}, status_code=400)
    from pathlib import Path
    if not Path(repo).is_dir():
        return JSONResponse(
            {"error": f"not a repo path: {repo!r} — use the full path from /api/repos, not the short name"},
            status_code=400,
        )
    brief = str(body.get("brief") or "").strip()
    issue = str(body.get("issue") or "").strip()
    if issue:
        info = repos.resolve_issue(issue, repo)
        if info.get("error"):
            return JSONResponse(info, status_code=400)
        block = f"{info['title']}\n\n{info['body']}\n\nGitHub issue: {info['url']}".strip()
        brief = f"{brief}\n\n{block}".strip() if brief else block
    if not brief:
        return JSONResponse({"error": "provide a GitHub issue or a brief"}, status_code=400)
    try:
        tid = dispatch.dispatch(repo, brief)
    except Exception as e:  # noqa: BLE001 - surface dispatch failures to the UI
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
    return JSONResponse({"id": tid})


async def api_suggestions(_: Request) -> JSONResponse:
    """Pending machine-discovered candidate rules (issue #7) — session_miner.py
    writes these; the board is the only path that turns one into a durable rule
    (captain-gated, same trust boundary as the old manual promote_rule)."""
    return JSONResponse({"suggestions": store.list_rule_suggestions(status="pending")})


async def api_suggestion_approve(req: Request) -> JSONResponse:
    import rules
    sid = int(req.path_params["sid"])
    s = store.get_rule_suggestion(sid)
    if not s:
        return JSONResponse({"error": "unknown suggestion"}, status_code=404)
    if s["status"] != "pending":
        return JSONResponse({"error": f"already {s['status']}"}, status_code=400)
    added = rules.add_rule(s["repo"], s["text"])
    store.decide_rule_suggestion(sid, "approved")
    return JSONResponse({"ok": True, "added": added})


async def api_suggestion_reject(req: Request) -> JSONResponse:
    sid = int(req.path_params["sid"])
    s = store.get_rule_suggestion(sid)
    if not s:
        return JSONResponse({"error": "unknown suggestion"}, status_code=404)
    if s["status"] != "pending":
        return JSONResponse({"error": f"already {s['status']}"}, status_code=400)
    store.decide_rule_suggestion(sid, "rejected")
    return JSONResponse({"ok": True})


# ── PWA: installable board (add-to-home-screen, offline shell) ──────────────
_MANIFEST = json.dumps({
    "name": "coxd — coxswain", "short_name": "coxd", "start_url": "/",
    "display": "standalone", "background_color": "#0d1117", "theme_color": "#0d1117",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml",
               "purpose": "any maskable"}],
})
_ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    '<rect width="512" height="512" rx="96" fill="#0d1117"/>'
    '<g fill="none" stroke="#3b82f6" stroke-width="22">'
    '<circle cx="256" cy="256" r="120"/><circle cx="256" cy="256" r="46"/>'
    '<g stroke-width="26">'
    '<line x1="256" y1="70" x2="256" y2="170"/><line x1="256" y1="342" x2="256" y2="442"/>'
    '<line x1="70" y1="256" x2="170" y2="256"/><line x1="342" y1="256" x2="442" y2="256"/>'
    '<line x1="124" y1="124" x2="195" y2="195"/><line x1="317" y1="317" x2="388" y2="388"/>'
    '<line x1="388" y1="124" x2="317" y2="195"/><line x1="195" y1="317" x2="124" y2="388"/>'
    '</g></g></svg>'
)
# network-first shell so the board updates instantly; API + SSE always bypass cache (live).
_SW = (
    "const C='coxd-v2';"
    "self.addEventListener('install',e=>{self.skipWaiting();"
    "e.waitUntil(caches.open(C).then(c=>c.addAll(['/','/icon.svg','/manifest.webmanifest'])))});"
    "self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));"
    "self.addEventListener('fetch',e=>{const u=new URL(e.request.url);"
    "if(u.pathname.startsWith('/api')||u.pathname==='/events')return;"
    "e.respondWith(fetch(e.request).then(r=>{const cp=r.clone();"
    "caches.open(C).then(c=>c.put(e.request,cp));return r}).catch(()=>caches.match(e.request)))});"
)


async def manifest(_: Request) -> Response:
    return Response(_MANIFEST, media_type="application/manifest+json")


async def service_worker(_: Request) -> Response:
    return Response(_SW, media_type="text/javascript")


async def icon(_: Request) -> Response:
    return Response(_ICON, media_type="image/svg+xml")


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
HUMAN_RESUME = "human-resume"  # queued by /resume; supervisor reads the note from events


def _allowed_hosts() -> set[str]:
    """Hostnames the board answers to. COXD_ALLOWED_HOSTS (comma-separated) adds to the
    defaults — e.g. the Traefik name. Ports are ignored."""
    hosts = {"localhost", "127.0.0.1", "coxd.home.manikantar.com"}
    extra = os.environ.get("COXD_ALLOWED_HOSTS", "")
    hosts.update(h.strip().lower() for h in extra.split(",") if h.strip())
    return hosts


def _hostname(value: str) -> str:
    """'https://Coxd.Example:8791/x' or 'coxd.example:8791' → 'coxd.example'."""
    v = value.strip().lower()
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]
    if v.startswith("["):  # [::1]:8791
        return v.split("]", 1)[0] + "]"
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


ADMIN_HEADER = "x-coxd-admin-token"      # local operator calls (root-only token file)
PROXY_HEADER = "x-coxd-proxy-secret"     # added by the Traefik middleware
USER_HEADER = "remote-user"              # set by the forward-auth service (Authelia)


def admin_token_path() -> str:
    return os.path.join(str(registry.home()), "admin.token")


def ensure_admin_token() -> str:
    """Return COXD_HOME/admin.token, creating it (mode 600) on first use. Workers run as
    another uid and a scratch COXD_HOME, so they cannot read it."""
    path = admin_token_path()
    try:
        with open(path) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_urlsafe(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    try:
        os.link(tmp, path)  # atomic create-if-absent; loser reads the winner's token
    except FileExistsError:
        pass
    finally:
        os.unlink(tmp)
    with open(path) as f:
        return f.read().strip()


def _eq(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def _owner_ok(headers: dict) -> bool:
    """COXD_OWNER_USER unset → True (unchanged). Otherwise a write needs either the
    local admin token, or the proxy secret AND the owner's Remote-User. No loopback
    exemption: workers share this container's network namespace."""
    owner = os.environ.get("COXD_OWNER_USER", "")
    if not owner:
        return True
    if _eq(headers.get(ADMIN_HEADER, ""), ensure_admin_token()):
        return True
    secret = os.environ.get("COXD_PROXY_SECRET", "")
    if not _eq(headers.get(PROXY_HEADER, ""), secret):
        return False  # fail closed, incl. secret unset
    return _eq(headers.get(USER_HEADER, ""), owner)


class _GuardMiddleware:
    """Refuse requests that a malicious web page could forge.

    The board has no login yet, so any page open in a LAN browser could POST to
    http://<nas>:8791/api/dispatch (a "simple" text/plain POST needs no CORS
    preflight) — and a dispatched brief runs an agent next to docker.sock. Two checks:
      - Host must be one of ours → defeats DNS rebinding.
      - On writes, a present Origin must be one of ours → defeats cross-site POSTs.
        (Browsers always send Origin on POST; curl/scripts send none and pass.)
    Real authentication comes later (Traefik forward-auth); this is the floor.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        allowed = _allowed_hosts()
        reason = None
        if _hostname(headers.get("host", "")) not in allowed:
            reason = "unknown host"
        elif scope.get("method", "GET") not in _SAFE_METHODS:
            origin = headers.get("origin")
            if origin is not None and _hostname(origin) not in allowed:
                reason = "cross-origin write"
            if reason is None and not _owner_ok(headers):
                reason = "owner login required"
        if reason:
            resp = JSONResponse({"error": f"forbidden: {reason}"}, status_code=403)
            return await resp(scope, receive, send)
        return await self.app(scope, receive, send)


app = Starlette(middleware=[Middleware(_GuardMiddleware)], routes=[
    Route("/", index),
    Route("/manifest.webmanifest", manifest),
    Route("/sw.js", service_worker),
    Route("/icon.svg", icon),
    Route("/api/tasks", api_tasks),
    Route("/api/task/{tid}/events", api_events),
    Route("/api/repos", api_repos),
    Route("/api/issues", api_issues),
    Route("/api/dispatch", api_dispatch, methods=["POST"]),
    Route("/api/task/{tid}/plan", api_task_plan),
    Route("/api/agy/queue", api_agy_queue),
    Route("/api/task/{tid}/retry", api_task_retry, methods=["POST"]),
    Route("/api/task/{tid}/agy-choice", api_task_choice, methods=["POST"]),
    Route("/api/task/{tid}/agy-status", api_task_agy_status, methods=["POST"]),
    Route("/api/task/{tid}/stop", api_task_stop, methods=["POST"]),
    Route("/api/task/{tid}/resume", api_task_resume, methods=["POST"]),
    Route("/api/task/{tid}/deploy", api_task_deploy, methods=["POST"]),
    Route("/api/repo/{repo}/approve-auto-merge", api_repo_approve_auto, methods=["POST"]),
    Route("/api/task/{tid}/archive", api_task_archive, methods=["POST"]),
    Route("/api/task/{tid}/unarchive", api_task_unarchive, methods=["POST"]),
    Route("/api/restart", api_restart, methods=["POST"]),
    Route("/api/factory", api_factory, methods=["GET", "POST"]),
    Route("/api/suggestions", api_suggestions),
    Route("/api/suggestion/{sid}/approve", api_suggestion_approve, methods=["POST"]),
    Route("/api/suggestion/{sid}/reject", api_suggestion_reject, methods=["POST"]),
    Route("/events", sse),
])

_HTML = """<!doctype html><html><head><meta charset=utf-8><title>coxd</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=theme-color content="#0d1117"><link rel=manifest href="/manifest.webmanifest">
<link rel=icon href="/icon.svg"><link rel=apple-touch-icon href="/icon.svg">
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-status-bar-style content=black-translucent>
<meta name=apple-mobile-web-app-title content=coxd>
<script>if('serviceWorker'in navigator)addEventListener('load',()=>navigator.serviceWorker.register('/sw.js').catch(()=>{}))</script>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#2a3140;--fg:#e6edf3;--dim:#8b949e;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#3b82f6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:10px;align-items:center}
h1{font-size:15px;margin:0}.pill{font-size:12px;color:var(--dim);background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:3px 10px}
.pill b{color:var(--fg)}.spacer{flex:1}#conn{color:var(--dim)}main{padding:16px;max-width:820px;margin:0 auto;display:grid;gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}.card.needs{border-color:#5a4310}
.row{display:flex;gap:10px;align-items:center}.badge{font-size:11px;font-weight:600;padding:2px 8px;border-radius:6px;text-transform:uppercase}
.b-work{background:#10233b;color:#6cb6ff}.b-warn{background:#2a2110;color:var(--warn)}.b-ok{background:#0f2417;color:var(--ok)}.b-bad{background:#2b1213;color:var(--bad)}
.tid{font-weight:600;word-break:break-all}.meta{color:var(--dim);font-size:12px;margin-top:2px}.last{font-size:12.5px;margin-top:6px;color:var(--fg);opacity:.85}
.agychoice ul{margin:6px 0;padding-left:18px}.chip{border-radius:8px;padding:1px 7px;font-size:11px;color:#fff;background:#888}.chip.sev-critical{background:#c0392b}.chip.sev-major{background:#d68910}.chip.sev-minor{background:#7f8c8d}.chip.budget{background:#b8860b;color:#fff}.lane{border-radius:4px;padding:1px 6px;font-size:10px;font-weight:700;letter-spacing:.5px;color:#fff;background:#555}.lane.agy{background:#138d90}.agybox{border:2px solid #138d90;border-radius:8px;padding:10px;margin:8px 0}.agybox code{display:block;font-family:ui-monospace,monospace;font-size:14px;padding:6px;background:#0006;margin:6px 0;user-select:all}.agyplan{white-space:normal;max-height:300px;overflow:auto;padding:6px;border:1px solid #444;margin-top:6px;font-size:12px}.agyplan pre{background:#0006;padding:6px;overflow:auto}.agyc{font-size:15px;padding:10px 16px;flex:1}.agyc small{font-size:11px;opacity:.8}
#scwrap{border-bottom:1px solid var(--line);background:var(--panel)}#scorecard summary{cursor:pointer;font-weight:600;user-select:none}
.sctbl{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}.sctbl th,.sctbl td{text-align:right;padding:4px 6px;border-bottom:1px solid var(--line)}.sctbl th:first-child,.sctbl td:first-child,.sctbl th:nth-child(2),.sctbl td:nth-child(2){text-align:left}.sctbl tr.sctot td{font-weight:600;border-top:2px solid var(--line)}
.pbar-row{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--dim);margin-top:4px}.pbar-lbl{min-width:130px}.pbar{display:flex;flex:1;height:8px;border-radius:4px;overflow:hidden;background:var(--bg);border:1px solid var(--line);max-width:260px}.pb-plan{background:#8957e5}.pb-implement{background:#3b82f6}.pb-fix{background:#d29922}.pb-review{background:#3fb950}.pbar-leg{display:flex;gap:10px;font-size:11px;color:var(--dim);margin-top:4px}.pbar-leg i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px;vertical-align:middle}.spark{display:inline-flex;align-items:center;gap:6px;margin-left:10px;vertical-align:middle}.unk-lbl{border-bottom:1px dotted var(--dim);cursor:help}
.retryrow{display:flex;gap:6px;align-items:center;margin-top:8px;flex-wrap:wrap}.retryrow button{margin-top:0}.retryrow input{flex:1;min-width:160px;padding:5px 8px}.rsmsg{font-size:12px;color:var(--dim)}
.mdl{display:block;text-transform:none;font-size:10px;color:#8b949e}.pipe{display:flex;align-items:center;margin-top:10px}.step{display:flex;flex-direction:column;align-items:center;flex:0 0 auto;font-size:10px;text-transform:uppercase;color:#6e7681;cursor:pointer;padding:2px 4px;border-radius:6px;transition:background .15s}.step:hover{background:#1c2333}
.dot{width:22px;height:22px;border-radius:50%;border:1px solid var(--line);display:flex;align-items:center;justify-content:center;font-size:11px;margin-bottom:3px;transition:background .3s,border-color .3s,color .3s,transform .3s}
.step.done .dot{background:#0f2417;border-color:var(--ok);color:var(--ok);animation:pop .35s ease}.step.done{color:var(--ok)}
.step.active .dot{border-color:var(--acc);color:#6cb6ff;background:#10233b;animation:p 1.4s infinite}.step.active{color:#6cb6ff}
.step.error .dot{border-color:var(--bad);color:var(--bad);background:#2b1213;animation:shake .4s ease}.step.error{color:var(--bad)}
.seg{flex:1;height:2px;background:var(--line);min-width:14px;margin-top:11px;position:relative;overflow:hidden}
.seg.on::after{content:'';position:absolute;inset:0;background:var(--ok);animation:fill .5s ease forwards}
.card{animation:cardIn .25s ease}.card.flash{animation:flash 1s ease}
@keyframes p{0%{box-shadow:0 0 0 0 rgba(59,130,246,.5)}70%,100%{box-shadow:0 0 0 6px rgba(59,130,246,0)}}
@keyframes pop{0%{transform:scale(.6)}60%{transform:scale(1.15)}100%{transform:scale(1)}}
@keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-3px)}75%{transform:translateX(3px)}}
@keyframes fill{from{transform:translateX(-100%)}to{transform:translateX(0)}}
@keyframes cardIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
@keyframes flash{0%{background:#1c2b1c}100%{background:var(--panel)}}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--fg);border-radius:8px;padding:5px 11px;margin-top:10px}
.feed{margin-top:10px;border-top:1px solid var(--line);padding-top:8px;font-family:ui-monospace,monospace;font-size:12px;color:var(--dim);white-space:pre-wrap;display:none}
.feed.open{display:block}.empty{color:var(--dim);text-align:center;padding:40px}
#dp{border-bottom:1px solid var(--line);background:var(--panel)}.dform{max-width:820px;margin:0 auto;padding:14px 16px;display:grid;gap:8px}
input,select,textarea{font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:7px 10px;width:100%}
textarea{resize:vertical}.drow{display:flex;gap:8px}.drow input{flex:1}#dgo.go{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:500}
#dmsg{font-size:13px;color:var(--dim);align-self:center}
#foff{background:#b3261e;color:#fff;font-weight:600;text-align:center;padding:6px;letter-spacing:.05em}#foff[hidden]{display:none}
#fspend{background:#b8860b;color:#fff;font-weight:600;text-align:center;padding:6px}#fspend[hidden]{display:none}
.offline{text-align:center;padding:40px 16px}.offline h2{margin:0 0 8px}</style></head><body>
<div id=fspend hidden></div>
<div id=foff hidden>⏸ FACTORY OFF — nothing new will start; running tasks finish</div>
<header><h1>▟ coxd</h1><span class=pill id=hostchip hidden></span><span class=pill id=pn>needs you <b>0</b></span><span class=pill id=pa>running <b>0</b><span id=pcap></span></span><span class=pill id=pi hidden></span><span class=pill id=pd hidden></span><span class=pill id=pm hidden></span><button class=pill id=lf title="Filter by lane">lane: all</button>
<button id=ab class=pill hidden>archived <b>0</b></button>
<span class=spacer></span><button id=fb title="Factory ON = picks up queued work. OFF = starts nothing new; running tasks finish.">Factory …</button><button id=rb title="Restart the coxd process in place — refuses while anything is active">↻ Restart cox</button><button id=nb>+ Dispatch</button><span id=conn class=pill>connecting…</span></header>
<section id=dp hidden><div class=dform>
<select id=drepo><option value="">— pick a repo —</option></select>
<div class=drow><input id=diss placeholder="issue URL or # (auto-fills the brief)"><button id=dload>List issues</button></div>
<select id=disslist hidden></select>
<textarea id=dbrief rows=3 placeholder="brief (blank = use the issue)"></textarea>
<div class=drow><button id=dgo class=go>Dispatch</button><span id=dmsg></span></div>
</div></section>
<section id=mspend hidden><div class=dform><div id=spendbody></div></div></section>
<section id=scwrap hidden><div class=dform><details id=scorecard><summary>Since-start scorecard</summary><div id=scbody></div></details></div></section>
<section id=sug hidden><div class=dform>
<div class=row><b>Suggested rules</b><span class=spacer></span><button id=sugtoggle>Hide</button></div>
<div id=suglist></div>
</div></section>
<main id=board><div class=empty>Loading…</div></main>
<script>
const open=new Set(),esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));let STAGES=['implement','gate','review','merge'],sig=null,spSig=null,scSig=null;
const UNK_LABEL='before model tracking',UNK_TIP='older result events have no model data';
const STAGE_TIP=[
 'IMPLEMENT — the worker (Claude) writes code in an isolated git worktree and commits. It never pushes; the control plane does that later.',
 'GATE — deterministic checks (migrations, tests, lint, build) run against the repo\\'s real toolchain. Must pass before the paid review runs. A failure here resumes the SAME worker session with the exact error to fix, up to 2 rounds.',
 'REVIEW — one pass from a second model (model shown on the chip) against the full diff since the branch point. Checks correctness/security/contract; verdict is approve, fix (resumes the worker once), or reject (stops for you).',
 'MERGE — pushes the branch, opens the PR, then watches its CI checks: known flakes get auto-retried (up to 2x), a real-looking failure or one touching e2e/test files stops for you. Passing CI still waits for YOUR merge click — that\\'s the one standing human gate.',
];
const PLAN_TIP='PLAN — a planner model reads the repo and writes the implementation plan the worker then follows (plan_first). Its model and cost are shown here; View plan expands the plan itself.';
const BADGE={queued:['b-work','queued'],provisioning:['b-work','provisioning'],working:['b-work','working'],gating:['b-work','gating'],fixing:['b-work','fixing'],reviewing:['b-work','reviewing'],shipping:['b-work','shipping'],'ci-watching':['b-work','watching CI'],pr_ready:['b-warn','ready to merge'],needs_human:['b-warn','needs you'],landed:['b-ok','landed'],waiting_quota:['b-warn','waiting quota'],planning:['b-work','planning'],waiting_agy:['b-warn','needs you'],needs_choice:['b-warn','choose']};
function pipe(st,models,names,notes,planFirst){models=models||{};notes=notes||{};const ST=names||STAGES,AG=!!names&&!planFirst;let h='<div class=pipe>';for(let k=0;k<ST.length;k++){let c,ic;if(k<st.i){c='done';ic='✓'}else if(k===st.i){c=st.status==='done'?'done':st.status==='error'?'error':'active';ic=st.status==='error'?'✕':(c==='done'?'✓':k+1)}else{c='';ic=k+1}
h+='<div class="step '+c+'"'+(AG?'':' data-stage="'+(planFirst?k-1:k)+'" title="'+esc(planFirst?(k?STAGE_TIP[k-1]:PLAN_TIP):STAGE_TIP[k])+'"')+'><span class=dot>'+ic+'</span>'+ST[k]+(AG&&k===1?'<span class=mdl>you'+(notes[1]?' · '+esc(notes[1]):'')+'</span>':models[k]?'<span class=mdl>'+esc(models[k])+'</span>':'')+'</div>';if(k<ST.length-1)h+='<span class="seg'+(k<st.i?' on':'')+'"></span>'}return h+'</div>'}
let LANE='all';
function mdHtml(src){const out=[];let code=false,list=false;const inl=x=>esc(x).replace(/`([^`]+)`/g,'<code style="display:inline;padding:0;margin:0;background:none">$1</code>').replace(/[*][*]([^*]+)[*][*]/g,'<b>$1</b>');
for(const ln of String(src||'').split('\\n')){if(/^```/.test(ln)){if(code){out.push('</pre>');code=false}else{if(list){out.push('</ul>');list=false}out.push('<pre>');code=true}continue}
if(code){out.push(esc(ln)+'\\n');continue}
const li=ln.match(/^\\s*(?:[-*]|\\d+[.])\\s+(.*)$/);if(li){if(!list){out.push('<ul>');list=true}out.push('<li>'+inl(li[1])+'</li>');continue}
if(list){out.push('</ul>');list=false}
const hd=ln.match(/^#{1,6}\\s+(.*)$/);if(hd){out.push('<h4>'+inl(hd[1])+'</h4>');continue}
if(ln.trim())out.push('<p>'+inl(ln)+'</p>')}
if(code)out.push('</pre>');if(list)out.push('</ul>');return out.join('')}
function usd(n){return '$'+(n||0).toFixed(2)}
function agyLiveHtml(a){const age=Math.max(0,Math.floor(Date.now()/1000-a.ts));if(age>=120)return '<div class="meta agylive">agy idle</div>';const m=Math.floor(a.elapsed_s/60);return '<div class="meta agylive">agy: '+esc(a.action)+' · step '+a.step+' · '+m+'m · '+Math.round(a.tokens/1000)+'k tokens · updated '+age+'s ago</div>'}
function planCardHtml(t){return t.lane==='claude'&&t.plan_md?'<div class=meta><button class=planb>View plan</button><div class=agyplan hidden>'+mdHtml(t.plan_md)+'</div></div>':''}
function agyCardHtml(t){let h='';const sp=t.claude_split;
if(t.state==='waiting_agy'&&t.agy_cmd){h+='<div class=agybox><b>Your turn: run on your Mac</b><code>'+esc(t.agy_cmd)+'</code><button class=agyb data-cmd="'+esc(t.agy_cmd)+'">Copy</button> <button class=planb>View plan</button><div class=agyplan hidden>'+mdHtml(t.plan_md)+'</div></div>'}
if(t.state==='waiting_agy'&&t.agy_status)h+=agyLiveHtml(t.agy_status);
if(t.agy_pickup)h+='<div class=meta>picked up your push '+esc(t.agy_pickup.sha)+' at '+new Date(t.agy_pickup.ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',timeZone:TZ})+(t.agy_round?' · round '+t.agy_round:'')+'</div>';
if(sp){const tot=t.claude_total!=null?t.claude_total:(sp.plan||0)+(sp.implement||0)+(sp.fix||0)+(sp.review||0);const parts=[['plan',sp.plan],['agy',sp.implement],['fix',sp.fix],['review',sp.review]].filter(([,v])=>v>0).map(([k,v])=>k+' '+usd(v));h+='<div class=meta>Claude '+usd(tot)+(parts.length?' ('+parts.join(' + ')+')':'')+(t.agy_tokens!=null?' · agy '+t.agy_tokens+' tokens':'')+'</div>'}
if(t.waiting_since&&t.state==='waiting_agy')h+='<div class=meta>waiting on you '+Math.max(0,Math.floor((Date.now()/1000-t.waiting_since)/60))+'m</div>';
return h}
function fmtTime(ts){if(!ts)return'--:--:--';return new Date(ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}
function fmtFindings(fs){if(!fs||!fs.length)return'    (none)';return fs.map(f=>'    - ['+(f.severity||'?')+'/'+(f.pillar||'?')+'] '+(f.file?f.file+(f.line?':'+f.line:'')+' — ':'')+(f.summary||'')).join('\\n')}
function feedLine(e,t){
if(e.kind==='state'){const st=String((e.data||{}).state||'').toUpperCase(),rs=(e.data||{}).reason;return'\\n'+t+'▸ '+st+(rs?' — '+rs:'')}
if(e.kind==='gate'){const g=e.data||{};if(g.passed)return t+'  ✓ gate passed'+(g.steps?' ('+Object.entries(g.steps).map(([k,v])=>k+':'+v).join(', ')+')':'');
return t+'  ✕ gate FAILED at '+g.failing+(g.reason?'\\n      reason: '+String(g.reason).slice(0,600).split('\\n').join('\\n      '):'')}
if(e.kind==='review'){const r=e.data||{};return t+'  review — outcome:'+r.outcome+' verdict:'+r.verdict+'\\n'+fmtFindings(r.findings)}
if(e.kind==='result'){const r=e.data||{};return t+'    ['+(r.model||'unknown')+'] $'+(typeof r.cost==='number'?r.cost.toFixed(3):'?')+' · '+(r.num_turns==null?'?':r.num_turns)+' turns'}
if(e.kind==='fix-trigger'){const f=e.data||{};return t+'  → sending back to fix (source: '+f.source+(f['fix-mode']?', fix-mode: '+f['fix-mode']:'')+')\\n'+
  (f.source==='gate'?'    '+f.failing+': '+String(f.reason||'').slice(0,400).split('\\n').join('\\n    '):fmtFindings(f.findings))}
return t+'    '+e.kind+' '+JSON.stringify(e.data).slice(0,90)}
const prevState=new Map();
const STATE_TO_STAGE={planning:-1,provisioning:0,working:0,fixing:0,gating:1,reviewing:2,shipping:3,'ci-watching':3};
function tagStages(events){let cur=0;return events.map(e=>{if(e.kind==='state'){const s=(e.data||{}).state;if(s in STATE_TO_STAGE)cur=STATE_TO_STAGE[s]}return Object.assign({},e,{_stage:cur})})}
const feedFilter=new Map();
async function loadFeed(id,el){const d=await(await fetch('/api/task/'+id+'/events')).json();
let evs=tagStages(d.events||[]);const fs=feedFilter.get(id);
let head='';if(fs!=null){evs=evs.filter(e=>e._stage===fs);head='── showing '+(fs<0?'PLAN':STAGES[fs].toUpperCase())+' only — click that step again to show everything ──\\n\\n'}
el.textContent=head+evs.map(e=>feedLine(e,'['+fmtTime(e.ts)+'] ')).join('\\n').replace(/^\\n/,'')}
let FACTORY_PAUSED=null;
let HOST='',GOT=false,offT=null;
function setHost(h){HOST=h;const c=document.getElementById('hostchip');c.textContent=h;c.hidden=!h}
let TZ='America/New_York';
const num=v=>typeof v==='number'&&isFinite(v)&&v>=0;
function dur(sec){if(!num(sec))return '';const m=Math.floor(sec/60);if(m<1)return Math.floor(sec)+'s';const h=Math.floor(m/60);return h?h+'h'+String(m%60).padStart(2,'0')+'m':m+'m'}
function fmt(opts,ts){try{return new Intl.DateTimeFormat('en-US',Object.assign({timeZone:TZ,hourCycle:'h23'},opts)).format(new Date(ts*1000))}catch(e){return new Intl.DateTimeFormat('en-US',opts).format(new Date(ts*1000))}}
function clock(ts){if(!num(ts))return '';const hm=fmt({hour:'2-digit',minute:'2-digit'},ts);return fmt({year:'numeric',month:'numeric',day:'numeric'},ts)===fmt({year:'numeric',month:'numeric',day:'numeric'},Date.now()/1000)?hm:fmt({month:'short',day:'numeric'},ts)+' '+hm}
function ago(ts){return num(ts)?dur(Date.now()/1000-ts)+' ago':''}
function timingLine(t){const m=t.timing;if(!m||typeof m!=='object')return '';const p=[];
if(t.state==='waiting_quota'&&num(t.resume_at)){const r=t.resume_at-Date.now()/1000;p.push('paused · resumes '+clock(t.resume_at)+(r>0?' (in '+dur(r)+')':''))}
if(t.state==='queued'&&num(m.queued_at))p.push('queued '+dur(Date.now()/1000-m.queued_at));
else if(m.in_progress&&num(m.started_at)&&t.state!=='waiting_quota')p.push('running '+dur(Date.now()/1000-m.started_at));
else if(num(m.started_at)){p.push('started '+clock(m.started_at));if(num(m.finished_at))p.push('finished '+clock(m.finished_at));
if(num(m.wall_s)&&m.wall_s>0){let d=dur(m.wall_s);const x=[];if(num(m.paused_quota_s)&&m.paused_quota_s>0)x.push(dur(m.paused_quota_s)+' paused · usage limit');d+=x.length?' ('+x.join(', ')+')':'';p.push(d)}}
if(num(m.review_rounds)&&m.review_rounds>0)p.push(m.review_rounds+' review round'+(m.review_rounds===1?'':'s'));
if(!p.length)return '';
const tip=[num(m.started_at)?'started '+ago(m.started_at):'',num(m.finished_at)?'finished '+ago(m.finished_at):''].filter(Boolean).join(', ');
return '<div class=tm'+(t.state==='queued'&&num(m.queued_at)?' data-queued='+m.queued_at:m.in_progress&&num(m.started_at)&&t.state!=='waiting_quota'?' data-run='+m.started_at:'')+(t.state==='waiting_quota'&&num(t.resume_at)?' data-resume='+t.resume_at:'')+' title="'+esc(tip)+'" style="font-size:12px;color:var(--dim);margin-top:2px">'+esc(p.join(' · '))+'</div>'}
setInterval(()=>{for(const e of document.querySelectorAll('.tm[data-queued]'))e.textContent=e.textContent.replace(/queued \\S+/,'queued '+dur(Date.now()/1000-parseFloat(e.dataset.queued)));
for(const e of document.querySelectorAll('.tm[data-run]'))e.textContent=e.textContent.replace(/running \\S+/,'running '+dur(Date.now()/1000-parseFloat(e.dataset.run)));
for(const e of document.querySelectorAll('.tm[data-resume]')){const r=parseFloat(e.dataset.resume)-Date.now()/1000;e.textContent=e.textContent.replace(/ \\(in [^)]*\\)/,r>0?' (in '+dur(r)+')':'')}},1000);
function showOffline(){if(GOT)return;sig=null;document.getElementById('conn').textContent='offline';const m=document.getElementById('board');m.innerHTML='<div class="empty offline" id=offline-panel><h2>coxd is not running on this machine</h2>'+(HOST?'<div class=meta>host: '+esc(HOST)+'</div>':'')+'<p><button id=retry>Retry</button></p></div>';document.getElementById('retry').onclick=()=>{m.innerHTML='<div class=empty>Loading…</div>';startWatch()}}
function startWatch(){clearTimeout(offT);offT=setTimeout(showOffline,5000);fetch('/api/factory').then(r=>{if(!r.ok)throw 0;return r.json()}).then(renderFactory).catch(()=>{clearTimeout(offT);showOffline()});connect()}
function sparkHtml(tr){if(!Array.isArray(tr)||!tr.length)return '';const vals=tr.filter(v=>typeof v==='number'&&isFinite(v));const tip=tr.map((v,i)=>'W-'+(tr.length-1-i)+': '+(typeof v==='number'&&isFinite(v)?'$'+v.toFixed(2):'—')).join(' · ');if(!vals.length)return '<span class="spark meta" title="'+esc(tip)+'">8w $/landed: —</span>';const mx=Math.max(...vals,0.01),w=88,h=18,bw=8,gap=3;const rects=tr.map((v,i)=>{const x=i*(bw+gap);if(typeof v!=='number'||!isFinite(v))return '<rect x="'+x+'" y="'+(h-2)+'" width="'+bw+'" height="2" fill="#2a3140"/>';const bh=Math.max(2,Math.round((v/mx)*(h-2)));return '<rect x="'+x+'" y="'+(h-bh)+'" width="'+bw+'" height="'+bh+'" rx="1" fill="#3b82f6"/>'}).join('');const last=vals[vals.length-1];return '<span class="spark meta" title="'+esc(tip)+'">8w $/landed <svg width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'" aria-label="8-week Claude cost per landed task">'+rects+'</svg> $'+last.toFixed(2)+'</span>'}
function phaseBarsHtml(pb){if(!pb||typeof pb!=='object')return '';const PS=['plan','implement','fix','review'];const rows=[];for(const[plab,pd]of[['month',pb.month],['all-time',pb.all_time]]){if(!pd||typeof pd!=='object')continue;for(const ln of['claude','agy']){const d=pd[ln];if(!d||!(d.total>0))continue;const bp=d.by_phase||{};const tip=PS.map(p=>p+' $'+(bp[p]||0).toFixed(2)).join(' · ');const segs=PS.map(p=>{const v=bp[p]||0;return v>0?'<span class="pb-'+p+'" style="width:'+(v/d.total*100).toFixed(1)+'%"></span>':''}).join('');rows.push('<div class=pbar-row title="'+esc(plab+' · '+ln+': '+tip)+'"><span class=pbar-lbl>'+esc(plab+' · '+ln)+' ($'+d.total.toFixed(2)+')</span><span class=pbar>'+segs+'</span></div>')}}if(!rows.length)return '';return rows.join('')+'<div class=pbar-leg>'+PS.map(p=>'<span><i class="pb-'+p+'"></i>'+esc(p)+'</span>').join('')+'</div>'}
function renderSpend(sp,pb,tr){const el=document.getElementById('mspend');if(!sp){el.hidden=true;spSig=null;return}el.hidden=false;const sk=JSON.stringify([sp,pb,tr]);if(sk===spSig)return;spSig=sk;const tip=sp.unknown_tip||UNK_TIP;const fmod=o=>Object.entries(o||{}).map(([m,v])=>(m===UNK_LABEL?'<span class=unk-lbl title="'+esc(tip)+'">'+esc(m)+'</span>':esc(m))+' $'+v.toFixed(2)).join(' · ')||'—';const fm=o=>Object.entries(o||{}).map(([m,v])=>esc(m)+' $'+v.toFixed(2)).join(' · ')||'—';document.getElementById('spendbody').innerHTML='<b>Spend this month</b> $'+(sp.total||0).toFixed(2)+sparkHtml(tr)+'<div class=meta>by model: '+fmod(sp.by_model)+'</div><div class=meta>by phase: '+fm(sp.by_phase)+'</div>'+phaseBarsHtml(pb)}
function renderScorecard(rows){const wrap=document.getElementById('scwrap'),body=document.getElementById('scbody');if(!Array.isArray(rows)||!rows.length){wrap.hidden=true;scSig=null;return}wrap.hidden=false;const sk=JSON.stringify(rows);if(sk===scSig)return;scSig=sk;const pct=(v,p,t)=>v==null?'—':v.toFixed(0)+'%'+(t!=null?' ('+p+'/'+t+')':'');const mon=v=>v==null?'—':'$'+v.toFixed(2);const strk=r=>r.streak_current==null?'—':r.streak_current+'/'+r.streak_best;const trs=rows.map(r=>'<tr'+(r.is_total?' class=sctot':'')+'><td>'+esc(r.repo)+'</td><td>'+esc(r.lane)+'</td><td>'+r.landed+'</td><td>'+r.needs_you+'</td><td>'+esc(pct(r.first_review_pct,r.first_review_passed,r.first_review_total))+'</td><td>'+esc(pct(r.first_gate_pct,r.first_gate_passed,r.first_gate_total))+'</td><td>'+esc(mon(r.median_cost))+'</td><td>'+esc(mon(r.total_cost))+'</td><td>'+esc(strk(r))+'</td></tr>').join('');body.innerHTML='<table class=sctbl><thead><tr><th>Repo</th><th>Lane</th><th>Landed</th><th>Needs you</th><th>1st review</th><th>1st gate</th><th>Median $</th><th>Total $</th><th>Streak</th></tr></thead><tbody>'+trs+'</tbody></table>'}
function renderFactory(f){if(!f)return;FACTORY_PAUSED=!!f.paused;document.getElementById('foff').hidden=!f.paused;document.getElementById('pcap').textContent=f.concurrency?' / '+f.concurrency:'';if(f.host)setHost(f.host);const b=document.getElementById('fb');b.textContent=f.paused?'⏸ Factory OFF':'▶ Factory ON';b.style.background=f.paused?'#5a2a2a':'#1f4d2b';}
function agyChoiceHtml(c){const chips=c.findings.map(f=>'<li><span class="chip sev-'+esc(f.severity)+'">'+esc(f.severity)+'</span> '+esc(f.summary)+(f.file?' <code>'+esc(f.file)+(f.line?':'+esc(String(f.line)):'')+'</code>':'')+'</li>').join('');const dis=c.capped?' disabled title="'+esc(c.cap_reason||'')+'"':'';const cost=c.claude_cost!=null?'uses Claude quota, about $'+c.claude_cost.toFixed(2)+' last time':'uses Claude quota';return '<div class=agychoice><b>Review findings</b> (round '+c.round+')<ul>'+(chips||'<li>none listed</li>')+'</ul><div class=retryrow>'+(c.send_enabled?'<button class=agyc data-choice=send'+dis+'>Send to agy<br><small>you run agy-next again</small></button>':'')+'<button class=agyc data-choice=claude'+dis+'>Let Claude fix<br><small>'+esc(cost)+'</small></button></div>'+(c.capped?'<div class=meta>'+esc(c.cap_reason)+'</div>':'')+(c.send_enabled?'':'<div class=meta>agy gets only the reviewed plan; fixes are done by Claude</div>')+'</div>'}
function render(s){s=Object.assign({},s,{tasks:s.tasks.filter(t=>LANE==='all'||t.lane===LANE)});s.needs_you=s.tasks.filter(t=>t.needs_you).length;s.active=s.tasks.filter(t=>t.active).length;GOT=true;clearTimeout(offT);{const sp=s.spend,pm=document.getElementById('pm'),bn=document.getElementById('fspend');if(sp){pm.hidden=false;pm.textContent='month $'+sp.mtd.toFixed(2);bn.hidden=!sp.banner;bn.textContent=sp.banner||''}else{pm.hidden=true;bn.hidden=true}}STAGES=s.stages||STAGES;renderFactory(s.factory);renderSpend(s.model_spend,s.phase_bars,s.cost_trend);renderScorecard(s.scorecard);{const pd=document.getElementById('pd'),q=s.pii;if(q){pd.hidden=false;pd.textContent=q.text;pd.style.borderColor=q.level==='ok'?'':(q.level==='due'?'#b8860b':'#a33');pd.style.color=q.level==='ok'?'':(q.level==='due'?'#e0b040':'#e07070')}else{pd.hidden=true}}{const pi=document.getElementById('pi'),n=s.intake;if(n&&n.at){pi.hidden=false;pi.textContent='intake: last poll '+new Date(n.at*1000).toLocaleTimeString()+' · took '+n.taken}else{pi.hidden=true}}document.querySelector('#pn b').textContent=s.needs_you;document.querySelector('#pa b').textContent=s.active;
const ab=document.getElementById('ab');if(s.archived){ab.hidden=false;ab.querySelector('b').textContent=s.archived;ab.title='landed/failed tasks older than '+s.archive_days+' days ('+(showArchived?'shown':'hidden')+')'}else{ab.hidden=true}
if(s.tz)TZ=s.tz;const k=JSON.stringify(s.tasks.map(t=>[LANE,t.id,t.lane,t.agy_pickup,t.claude_split,t.claude_total,t.waiting_since&&Math.floor(Date.now()/60000),t.agy_status&&[t.agy_status.ts,Math.floor(Date.now()/10000)],t.state,t.reason,t.cost,t.over_budget,t.plan_cost,t.plan_md&&t.plan_md.length,t.models,t.last,t.issue_url,t.resume_at,t.agy_round,t.quota_wait_at,t.timing&&[t.timing.started_at,t.timing.queued_at,t.timing.finished_at,t.timing.in_progress,t.timing.review_rounds,Math.floor((t.timing.paused_quota_s||0)/60)]]));if(k===sig){for(const id of open){const el=document.querySelector('.card[data-id="'+CSS.escape(id)+'"] .feed');if(el)loadFeed(id,el)}return}sig=k;
const changed=new Set();for(const t of s.tasks){if(prevState.has(t.id)&&prevState.get(t.id)!==t.state)changed.add(t.id);prevState.set(t.id,t.state)}
const b=document.getElementById('board');if(!s.tasks.length){b.innerHTML='<div class=empty>No tasks yet.</div>';return}let h='';for(const t of s.tasks){const[cls,lab]=BADGE[t.state]||['b-work',t.state];
const issueNum=t.issue_url?t.issue_url.split('/').pop():null;
h+='<div class="card'+(t.needs_you?' needs':'')+(changed.has(t.id)?' flash':'')+'" data-id="'+esc(t.id)+'"><div class=row><span class="lane '+(t.lane==='agy'?'agy':'')+'">'+(t.lane==='agy'?'AGY':'CLAUDE')+'</span><span class="badge '+cls+'">'+lab+'</span><span class=tid>'+esc(t.id)+'</span>'+
(t.over_budget?'<span class="chip budget" title="'+esc('Claude cost $'+(t.cost||0).toFixed(2)+' exceeds $'+(t.budget||3).toFixed(2)+' budget')+'">over budget</span>':'')+
(t.issue_url?'<a href="'+esc(t.issue_url)+'" target=_blank style="color:#6cb6ff;font-size:12px">#'+esc(issueNum)+'</a>':'')+'</div>'+
'<div class=meta>'+esc(t.repo)+(t.cost?' · $'+t.cost.toFixed(3)+(t.plan_cost!=null?' (plan $'+t.plan_cost.toFixed(3)+')':''):'')+(t.reason?' · '+esc(t.reason):'')+(t.pr_url?' · <a href="'+esc(t.pr_url)+'" target=_blank style="color:#6cb6ff">PR</a>':'')+'</div>'+timingLine(t)+pipe(t.stage,t.models,t.stages,t.agy_round?{1:'round '+t.agy_round}:null,t.lane==='claude'&&!!t.stages)+(t.last?'<div class=last>'+esc(t.last)+'</div>':'')+
(t.lane==='agy'?agyCardHtml(t):planCardHtml(t))+
(t.quota_wait_at?'<div class=meta>Review waits for Claude reset at '+esc(new Date(t.quota_wait_at*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',timeZone:TZ}))+'</div>':'')+
(t.choice?agyChoiceHtml(t.choice):'')+
(t.active?'<div class=retryrow><button class=spb data-pr="'+(t.pr_url?'1':'')+'" title="Cancel this task in place. Whatever is already committed is kept — lands at needs_human, same as any other stall, and Resume w/ note picks it back up.">Stop</button></div>':'')+
(t.state==='needs_human'?'<div class=retryrow>'+
 '<button class=rtb data-pr="'+(t.pr_url?'1':'')+'" title="Starts over from the original brief in a fresh worker call — discards this session\\'s context. Safe when nothing was committed yet (e.g. an auth/infra error before any real work happened).">Retry fresh</button>'+
 '<button class=rsb title="Keeps the existing session and any commits already made, and gives it your note as the next instruction. Cheaper and more targeted than Retry when real work already landed.">Resume w/ note</button>'+
 '<input class=rsnote hidden placeholder="what should it try differently?"><button class=rsgo hidden>Go</button><span class=rsmsg></span></div>':
 t.state==='pr_ready'&&t.resumable?'<div class=retryrow>'+
 '<button class=rsb title="Review found something? Sends your note to the same session; the fix is gated, re-reviewed and pushed to this same PR.">Send fixes w/ note</button>'+
 '<input class=rsnote hidden placeholder="what needs fixing?"><button class=rsgo hidden>Go</button><span class=rsmsg></span></div>':'')+
(t.approve_auto?'<div class=retryrow><button class=aab data-repo="'+esc(t.repo)+'" title="This repo earned a clean-merge streak. Approving lets coxd merge its PRs without you — only this button enables it.">Approve auto-merge</button><span class=aamsg></span></div>':'')+
(t.deployable?'<div class=retryrow><button class=depb data-repo="'+esc(t.repo)+'" title="Build + recreate this repo\\'s containers on the NAS at current main, then run migrations. Human-gated — coxd does the work; watch Events for progress.">Deploy ▸</button><span class=depmsg></span></div>':'')+
(t.archived?'<button class=arb data-mode=unarchive title="Bring this task back into the normal board view.">Unarchive</button>':t.archivable?'<button class=arb data-mode=archive title="Hide this completed task from the board now — same as it aging out on its own, just immediate.">Archive</button>':'')+
'<button class=fb>'+(open.has(t.id)?'Hide':'Events')+'</button><div class="feed'+(open.has(t.id)?' open':'')+'"></div></div>'}
b.innerHTML=h;for(const c of b.querySelectorAll('.card')){const id=c.dataset.id,f=c.querySelector('.feed');if(open.has(id))loadFeed(id,f);
c.querySelector('.fb').onclick=()=>{if(open.has(id)){open.delete(id);f.classList.remove('open')}else{open.add(id);f.classList.add('open');loadFeed(id,f)}c.querySelector('.fb').textContent=open.has(id)?'Hide':'Events'};
const arb=c.querySelector('.arb');if(arb)arb.onclick=async()=>{arb.disabled=true;try{const r=await(await fetch('/api/task/'+id+'/'+arb.dataset.mode,{method:'POST'})).json();if(r.error){alert(r.error);arb.disabled=false}else{sig=null}}catch(e){alert(e);arb.disabled=false}};
const aab=c.querySelector('.aab');if(aab)aab.onclick=async()=>{if(!confirm('Approve auto-merge for '+aab.dataset.repo+'?'))return;aab.disabled=true;try{const r=await(await fetch('/api/repo/'+encodeURIComponent(aab.dataset.repo)+'/approve-auto-merge',{method:'POST'})).json();if(r.error){alert(r.error);aab.disabled=false}else{sig=null}}catch(e){alert(e);aab.disabled=false}};
const depb=c.querySelector('.depb'),depmsg=c.querySelector('.depmsg');
if(depb)depb.onclick=async()=>{if(!confirm('Deploy '+depb.dataset.repo+' at current main to the NAS?\\n\\nThis builds images, recreates containers, and runs migrations — coxd runs the repo\\'s deploy script on the NAS.'))return;depb.disabled=true;depb.textContent='deploying…';depmsg.textContent='';try{const r=await(await fetch('/api/task/'+id+'/deploy',{method:'POST'})).json();if(r.error){depmsg.textContent='✗ '+r.error;depb.disabled=false;depb.textContent='Deploy ▸'}else{depmsg.textContent='building on the NAS — watch Events; you\\'ll get a ping when done';open.add(id);f.classList.add('open');c.querySelector('.fb').textContent='Hide';loadFeed(id,f)}}catch(e){depmsg.textContent='✗ '+e;depb.disabled=false;depb.textContent='Deploy ▸'}};
c.querySelectorAll('.step[data-stage]').forEach(stepEl=>{stepEl.onclick=()=>{const s=parseInt(stepEl.dataset.stage,10);
feedFilter.set(id,feedFilter.get(id)===s?undefined:s);open.add(id);f.classList.add('open');c.querySelector('.fb').textContent='Hide';loadFeed(id,f)}});
for(const ab of c.querySelectorAll('.agyc'))ab.onclick=async()=>{for(const x of c.querySelectorAll('.agyc'))x.disabled=true;try{const r=await(await fetch('/api/task/'+id+'/agy-choice',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({choice:ab.dataset.choice})})).json();if(r.error){alert(r.error);for(const x of c.querySelectorAll('.agyc'))x.disabled=false}else{sig=null}}catch(e){alert(e)}};
const agyb=c.querySelector('.agyb');if(agyb)agyb.onclick=async()=>{try{await navigator.clipboard.writeText(agyb.dataset.cmd);agyb.textContent='copied'}catch(e){const r=document.createRange();r.selectNodeContents(agyb.parentNode.querySelector('code'));const sl=getSelection();sl.removeAllRanges();sl.addRange(r);agyb.textContent='selected — press Ctrl/Cmd+C'}};
const planb=c.querySelector('.planb');if(planb)planb.onclick=()=>{const pl=c.querySelector('.agyplan');pl.hidden=!pl.hidden;planb.textContent=pl.hidden?'View plan':'Hide plan'};
const spb=c.querySelector('.spb');if(spb)spb.onclick=async()=>{if(!confirm('Stop this task now? Whatever is already committed is kept — it lands at needs_human and you can Resume w/ note to pick it back up.'))return;spb.disabled=true;spb.textContent='stopping…';try{const r=await(await fetch('/api/task/'+id+'/stop',{method:'POST'})).json();if(r.error){alert(r.error);spb.disabled=false;spb.textContent='Stop'}else{sig=null}}catch(e){alert(e)}};
const rtb=c.querySelector('.rtb');if(rtb)rtb.onclick=async()=>{const warn=rtb.dataset.pr?'This task already has an open PR — Retry fresh starts a SECOND implementation from scratch and will likely open a duplicate PR. Merge or close the existing PR first, or use Resume w/ note instead. Continue anyway?':'Retry fresh discards this session\\'s context and starts over from the original brief. Continue?';if(!confirm(warn))return;rtb.disabled=true;rtb.textContent='retrying…';try{const r=await(await fetch('/api/task/'+id+'/retry',{method:'POST'})).json();if(r.error){alert(r.error);rtb.disabled=false;rtb.textContent='Retry fresh'}else{sig=null}}catch(e){alert(e)}};
const rsb=c.querySelector('.rsb'),rsnote=c.querySelector('.rsnote'),rsgo=c.querySelector('.rsgo'),rsmsg=c.querySelector('.rsmsg');
if(rsb)rsb.onclick=()=>{rsb.hidden=true;rsnote.hidden=false;rsgo.hidden=false;rsnote.focus()};
if(rsgo)rsgo.onclick=async()=>{const note=rsnote.value.trim();if(!note){rsmsg.textContent='note required';return}rsgo.disabled=true;rsmsg.textContent='resuming…';
try{const r=await(await fetch('/api/task/'+id+'/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note})})).json();
if(r.error){rsmsg.textContent='✗ '+r.error;rsgo.disabled=false}else{sig=null}}catch(e){rsmsg.textContent='✗ '+e;rsgo.disabled=false}}}}
const nb=document.getElementById('nb'),dp=document.getElementById('dp'),drepo=document.getElementById('drepo'),disslist=document.getElementById('disslist'),dmsg=document.getElementById('dmsg');
nb.onclick=()=>{dp.hidden=!dp.hidden;if(!dp.hidden)loadRepos()};
async function loadRepos(){try{const d=await(await fetch('/api/repos')).json();drepo.innerHTML='<option value="">— pick a repo —</option>'+(d.repos||[]).map(r=>'<option value="'+esc(r.path)+'">'+esc(r.name)+'</option>').join('')}catch(e){}}
document.getElementById('dload').onclick=async()=>{if(!drepo.value){dmsg.textContent='pick a repo';return}dmsg.textContent='loading issues…';try{const d=await(await fetch('/api/issues?repo='+encodeURIComponent(drepo.value))).json();if(d.error){dmsg.textContent='✗ '+d.error;return}if(!(d.issues||[]).length){dmsg.textContent='no open issues';disslist.hidden=true;return}disslist.innerHTML='<option value="">— pick an issue ('+d.issues.length+') —</option>'+d.issues.map(i=>'<option value="'+esc(i.url)+'">#'+i.number+' '+esc(i.title)+'</option>').join('');disslist.hidden=false;dmsg.textContent=''}catch(e){dmsg.textContent='✗ '+e}};
disslist.onchange=()=>{if(disslist.value)document.getElementById('diss').value=disslist.value};
document.getElementById('dgo').onclick=async()=>{const body={repo:drepo.value,issue:document.getElementById('diss').value.trim(),brief:document.getElementById('dbrief').value.trim()};if(!body.repo){dmsg.textContent='pick a repo';return}if(!body.issue&&!body.brief){dmsg.textContent='pick an issue or write a brief';return}dmsg.textContent='dispatching…';try{const d=await(await fetch('/api/dispatch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();if(d.error){dmsg.textContent='✗ '+d.error;return}dmsg.textContent='✓ queued '+d.id;document.getElementById('diss').value='';document.getElementById('dbrief').value='';disslist.hidden=true;sig=null}catch(e){dmsg.textContent='✗ '+e}};
let showArchived=false,es=null;
function connect(){if(es)es.close();const q=showArchived?'?all=1':'';es=new EventSource('/events'+q);es.onopen=()=>{document.getElementById('conn').textContent='live'};es.onmessage=e=>render(JSON.parse(e.data));es.onerror=()=>{document.getElementById('conn').textContent='reconnecting…'};
fetch('/api/tasks'+q).then(r=>r.json()).then(render).catch(()=>{})}
document.getElementById('lf').onclick=()=>{LANE=LANE==='all'?'claude':LANE==='claude'?'agy':'all';document.getElementById('lf').textContent='lane: '+LANE;sig=null;connect()};document.getElementById('ab').onclick=()=>{showArchived=!showArchived;sig=null;connect()};
// --- session-mined rule suggestions (issue #7) — captain approves/rejects, never auto-applied ---
async function loadSuggestions(){try{const d=await(await fetch('/api/suggestions')).json();const list=d.suggestions||[];const sec=document.getElementById('sug'),ul=document.getElementById('suglist');
if(!list.length){sec.hidden=true;return}sec.hidden=false;
const byRepo={};for(const s of list){(byRepo[s.repo]=byRepo[s.repo]||[]).push(s)}
let h='';for(const repo of Object.keys(byRepo).sort()){h+='<div class=meta style="margin-top:8px"><b>'+esc(repo)+'</b></div>';
for(const s of byRepo[repo]){h+='<div class=card style="padding:10px;margin-top:6px"><div>'+esc(s.text)+'</div>'+
(s.evidence?'<div class=last style="opacity:.7">"'+esc(s.evidence)+'"</div>':'')+
'<div class=meta>'+esc(s.source||'')+'</div>'+
'<div class=retryrow><button class=sug-ok data-id="'+s.id+'">Approve</button><button class=sug-no data-id="'+s.id+'">Reject</button></div></div>'}}
ul.innerHTML=h;
ul.querySelectorAll('.sug-ok').forEach(b=>b.onclick=async()=>{b.disabled=true;try{const r=await(await fetch('/api/suggestion/'+b.dataset.id+'/approve',{method:'POST'})).json();if(r.error){alert(r.error);b.disabled=false}else{loadSuggestions()}}catch(e){alert(e);b.disabled=false}});
ul.querySelectorAll('.sug-no').forEach(b=>b.onclick=async()=>{b.disabled=true;try{const r=await(await fetch('/api/suggestion/'+b.dataset.id+'/reject',{method:'POST'})).json();if(r.error){alert(r.error);b.disabled=false}else{loadSuggestions()}}catch(e){alert(e);b.disabled=false}});
}catch(e){}}
document.getElementById('sugtoggle').onclick=()=>{const l=document.getElementById('suglist');l.hidden=!l.hidden;document.getElementById('sugtoggle').textContent=l.hidden?'Show':'Hide'};
loadSuggestions();setInterval(loadSuggestions,15000);
document.getElementById('fb').onclick=async()=>{const turnOff=!FACTORY_PAUSED;if(!confirm(turnOff?'Turn the factory OFF on this machine? Running tasks finish; nothing new starts.':'Turn the factory ON on this machine? It will start queued work.'))return;
try{const r=await(await fetch('/api/factory',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paused:turnOff})})).json();if(r.error){alert(r.error)}else{renderFactory(r)}}catch(e){alert(e)}};
document.getElementById('rb').onclick=async()=>{if(!confirm('Restart coxd now? Refuses if anything is active.'))return;
try{const r=await(await fetch('/api/restart',{method:'POST'})).json();if(r.error){alert(r.error)}else{alert('Restarting — the board will go blank for a few seconds.')}}catch(e){}};
startWatch();
</script></body></html>"""


if __name__ == "__main__":
    if os.environ.get("COXD_OWNER_USER"):
        ensure_admin_token()
    uvicorn.run(app, host="0.0.0.0", port=8791, log_level="warning")  # noqa: S104

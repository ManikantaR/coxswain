"""agy handoff lane 3/4: pick up the human-pushed `coxd-agy/<id>` branch.

Plain git only (ls-remote / fetch) plus store bookkeeping. coxd never executes,
signals or wraps agy; the only interface is the git branch and the board API.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import notify
import registry
import sandbox
import store

BRANCH_PREFIX = "coxd-agy/"
AGY_PICKUP = "agy-pickup"  # queued reason: a new head was found on the agy branch
MAX_FIX_ROUNDS = 2         # any mix of Send-to-agy / Let-Claude-fix / red-gate rounds
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SEV = ("high", "med", "low")  # the review schema: shown as critical / major / minor
_ISSUE_RE = re.compile(r"^GitHub issue:\s+\S+/issues/(\d+)\s*$", re.MULTILINE)


def issue_and_deps(brief: str | None) -> tuple[int | None, list[int | str]]:
    """Extract (issue_number, depends_on) from a task brief.

    Same-repo `Depends on #N` targets are stored as ints; cross-repo URL targets
    are stored as `"owner/repo#N"` so they never collide with local issue numbers.
    """
    if not brief:
        return None, []
    import intake  # lazy: avoids import coupling/cycles between intake and loop/agy_handoff
    m = _ISSUE_RE.search(brief)
    issue = int(m.group(1)) if m else None
    deps: list[int | str] = []
    seen: set[int | str] = set()
    for dm in intake._DEP_RE.finditer(brief):
        dep: int | str = int(dm.group(1)) if dm.group(1) else f"{dm.group(2)}#{int(dm.group(3))}"
        if dep not in seen:
            seen.add(dep)
            deps.append(dep)
    return issue, deps


def plan_files(task: dict | str) -> set[str]:
    """Return the planned files from the latest `plan` event for `task` (or task id)."""
    t = store.get_task(task) if isinstance(task, str) else task
    if not t:
        return set()
    evs = [e for e in store.events(t["id"]) if e["kind"] == "plan"]
    if not evs:
        return set()
    latest = max(
        enumerate(evs),
        key=lambda ie: ((ie[1].get("data") or {}).get("round") or 0, ie[0]),
    )[1]
    data = latest.get("data") if isinstance(latest.get("data"), dict) else {}
    if "files" in data and data["files"] is not None:
        return {f for f in data["files"] if isinstance(f, str) and f}
    import lane  # lazy: avoids pulling claude_agent_sdk into board/status imports
    return set(lane.extract_plan_files(data.get("plan_md") or ""))


_TRUE, _FALSE = ("1", "true", "yes", "on"), ("0", "false", "no", "off", "")


def send_enabled(repo: str) -> bool:
    """Owner decision (2026-11-15): agy only ever receives the human-reviewed plan, so
    "Send to agy" (AI-written follow-ups routed back to agy) is OFF by default. Per-repo
    registry key `agy_send_enabled` > env COXD_AGY_SEND_ENABLED > False. Anything that is
    not an explicit true value (bool or true/1/yes/on string) counts as off."""
    for cand in ((registry.load(repo) or {}).get("agy_send_enabled"),
                 os.environ.get("COXD_AGY_SEND_ENABLED")):
        if isinstance(cand, bool):
            return cand
        if isinstance(cand, str) and cand.strip().lower() in _TRUE + _FALSE[:-1]:
            return cand.strip().lower() in _TRUE
    return False


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=60)


def remote_head(repo_path: str, task_id: str) -> str | None:
    """Head SHA of origin's coxd-agy/<id>, or None (missing branch, network, bad output)."""
    try:
        r = _git(Path(repo_path), "ls-remote", "origin", f"refs/heads/{BRANCH_PREFIX}{task_id}")
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    sha = r.stdout.split()[0]
    return sha if _SHA.match(sha) else None


def last_seen(task: dict) -> str | None:
    seen = [e["data"].get("sha") for e in store.events(task["id"]) if e["kind"] == "agy-head"]
    return seen[-1] if seen else task.get("base_sha")


def rounds_used(task_id: str) -> int:
    return sum(1 for e in store.events(task_id) if e["kind"] == "fix-round")


def scan() -> list[str]:
    """Queue every waiting_agy task whose branch has a head we haven't seen. The
    compare-and-set claim makes each new commit trigger exactly one run."""
    queued = []
    for t in store.list_tasks():
        if t["state"] != "waiting_agy" or t.get("lane") != "agy-handoff" or not t.get("repo_path"):
            continue
        sha = remote_head(t["repo_path"], t["id"])
        if not sha or sha == last_seen(t):
            continue
        if store.cas_state(t["id"], "waiting_agy", "queued", AGY_PICKUP):
            store.append_event(t["id"], "agy-head", {"sha": sha})
            queued.append(t["id"])
    return queued


def fetch_into_worktree(task: dict) -> str | None:
    """Fetch the agy branch head (from the main checkout, which holds the creds) and
    point the task worktree at it. Records a newer head if the branch moved."""
    sha = remote_head(task["repo_path"], task["id"])
    if not sha:
        return None
    ref = f"refs/remotes/origin/{BRANCH_PREFIX}{task['id']}"
    f = _git(Path(task["repo_path"]), "fetch", "origin",
             f"+refs/heads/{BRANCH_PREFIX}{task['id']}:{ref}")
    if f.returncode != 0:
        return None
    if sha != last_seen(task):
        store.append_event(task["id"], "agy-head", {"sha": sha})
    applied = [e["data"].get("sha") for e in store.events(task["id"]) if e["kind"] == "agy-applied"]
    if applied and applied[-1] == sha:  # already there; a reset would drop Claude's own commits
        return sha
    r = sandbox.run_in_worktree(["git", "reset", "--hard", sha], task["worktree"])
    if r.returncode != 0:
        return None
    store.append_event(task["id"], "agy-applied", {"sha": sha})
    return sha


def format_findings(findings: list[dict]) -> str:
    """Markdown plan round from review findings, most severe first."""
    def sev(f):
        s = str(f.get("severity", "")).lower()
        return _SEV.index(s) if s in _SEV else len(_SEV)
    lines = ["## Fix these review findings", ""]
    for f in sorted(findings, key=sev):
        line = f":{f['line']}" if f.get("line") else ""
        where = f" ({f['file']}{line})" if f.get("file") else ""
        lines.append(f"- **{f.get('severity', 'note')}**{where}: "
                     f"{f.get('summary') or f.get('title') or f.get('detail') or f}")
    return "\n".join(lines) + "\n"


def next_round(task_id: str, plan_md: str, via: str, source: str) -> None:
    """Record a fix round and park at waiting_agy with `plan_md` as the next plan."""
    t = store.get_task(task_id)
    files = sorted(plan_files(t)) if t else []
    issue, depends_on = issue_and_deps(t.get("brief") if t else None)
    store.set_plan(task_id, plan_md, t.get("base_sha"))
    rnd = 1 + sum(1 for e in store.events(task_id) if e["kind"] == "plan")
    store.append_event(task_id, "fix-round", {"via": via, "source": source})
    store.append_event(task_id, "plan", {
        "plan_md": plan_md,
        "round": rnd,
        "branch": f"{BRANCH_PREFIX}{task_id}",
        "files": files,
        "issue": issue,
        "depends_on": depends_on,
    })
    store.set_state(task_id, "waiting_agy", f"run agy-next {task_id}")
    notify.notify_async("coxd · needs you (agy)", f"Run on your Mac: agy-next {task_id}")


def record_claude_round(task_id: str, source: str) -> None:
    """Count an automatic Claude fix round against the same cap as agy rounds."""
    store.append_event(task_id, "fix-round", {"via": "claude", "source": source})


def choose(task_id: str, choice: str) -> tuple[bool, str]:
    """Board buttons on a needs_choice task. Returns (ok, error)."""
    t = store.get_task(task_id)
    if t is None or t.get("lane") != "agy-handoff":
        return False, "no such agy-handoff task"
    if t["state"] != "needs_choice":
        return False, f"cannot choose from state '{t['state']}'"
    if choice not in ("send", "claude"):
        return False, "choice must be 'send' or 'claude'"
    if rounds_used(task_id) >= MAX_FIX_ROUNDS:
        return False, "round cap reached"
    if choice == "send":
        if not send_enabled(t["repo"]):
            return False, "Send to agy is disabled by owner"
        findings = store.findings_of(t)
        if not store.cas_state(task_id, "needs_choice", "planning"):  # claim vs double click
            return False, "already chosen"
        next_round(task_id, format_findings(findings), "agy", "review")
        return True, ""
    if not store.cas_state(task_id, "needs_choice", "queued", "agy-claude-fix"):
        return False, "already chosen"
    store.append_event(task_id, "fix-round", {"via": "claude", "source": "review"})
    store.append_event(task_id, "agy-claude-fix", {})
    return True, ""


def claude_fix_pending(task_id: str) -> bool:
    evs = [e["kind"] for e in store.events(task_id)]
    return evs.count("agy-claude-fix") > evs.count("agy-claude-fix-start")


def quota_wait_at(task: dict) -> float | None:
    """resume time when this task is parked on a Claude usage limit (board banner)."""
    if "review" not in (task.get("reason") or ""):
        return None
    resume_at = task.get("resume_at")
    if task["state"] == "waiting_quota" and resume_at and resume_at > time.time():
        return task["resume_at"]
    return None

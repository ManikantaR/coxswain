"""agy handoff lane 3/4: pick up the human-pushed `coxd-agy/<id>` branch.

Plain git only (ls-remote / fetch) plus store bookkeeping. coxd never executes,
signals or wraps agy; the only interface is the git branch and the board API.
"""
from __future__ import annotations

import json
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


def _files_from_events(evs: list[dict]) -> set[str]:
    """Return the planned files from the latest `plan` event in `evs`."""
    plan_evs = [e for e in evs if e.get("kind") == "plan"]
    if not plan_evs:
        return set()
    latest = max(
        enumerate(plan_evs),
        key=lambda ie: ((ie[1].get("data") or {}).get("round") or 0, ie[0]),
    )[1]
    data = latest.get("data") if isinstance(latest.get("data"), dict) else {}
    if "files" in data and data["files"] is not None:
        return {f for f in data["files"] if isinstance(f, str) and f}
    import lane  # lazy: avoids pulling claude_agent_sdk into board/status imports
    return set(lane.extract_plan_files(data.get("plan_md") or ""))


def plan_files(task: dict | str) -> set[str]:
    """Return the planned files from the latest `plan` event for `task` (or task id)."""
    t = store.get_task(task) if isinstance(task, str) else task
    if not t:
        return set()
    return _files_from_events(store.events(t["id"]))


MODEL_SMALL = "gemini-4-hydrogen-high"
MODEL_DEFAULT = "gemini-4-argon-high"
_ISSUE_LABEL = {"size:S": MODEL_SMALL}
_STARTED_STATES = {
    "queued", "planning", "provisioning", "working", "fixing", "gating",
    "reviewing", "shipping", "ci-watching", "pr_ready", "needs_choice", "waiting_quota",
}
_GH_TIMEOUT_S = 10.0
_PR_FILES_CACHE: dict[tuple[str, int, str], set[str]] = {}
_PR_FILES_CACHE_MAX = 256
_ISSUE_LABEL_CACHE: dict[tuple[str, int], tuple[float, set[str]]] = {}
_ISSUE_LABEL_TTL_S = 60.0
_ISSUE_LABEL_CACHE_MAX = 512


def _round_start(evs: list[dict]) -> float:
    """Timestamp of the latest `plan` event (start of the current agy round), or 0.0."""
    ts = 0.0
    for e in evs:
        if e.get("kind") == "plan":
            t = float(e.get("ts") or 0)
            if t >= ts:
                ts = t
    return ts


def _round_events(evs: list[dict]) -> list[dict]:
    """Events belonging to the current round (at or after the latest `plan` event)."""
    start = _round_start(evs)
    return [e for e in evs if float(e.get("ts") or 0) >= start]


def suggest_model(labels: set[str] | list[str] | tuple[str, ...] | None = None) -> str:
    """Suggest an agy model from issue labels (size:S -> Hydrogen, else Argon)."""
    if labels:
        for label, model in _ISSUE_LABEL.items():
            if label in labels:
                return model
    return MODEL_DEFAULT


def _title_from_task(task: dict) -> str:
    src = task.get("intent") or task.get("brief") or ""
    lines = [ln.strip() for ln in src.splitlines() if ln.strip()]
    if lines and lines[0].startswith("## Learned rules"):
        lines = [ln for ln in lines[1:] if not ln.startswith("- ")]
    if not lines:
        return ""
    return lines[0].lstrip("#").strip()[:120]


def _is_candidate(task: dict, round_evs: list[dict]) -> bool:
    return (
        task.get("lane") == "agy-handoff"
        and task.get("state") == "waiting_agy"
        and not task.get("archived_at")
        and not any(e.get("kind") == "agy-head" for e in round_evs)
    )


def _is_started(task: dict, round_evs: list[dict]) -> bool:
    if task.get("lane") != "agy-handoff" or task.get("archived_at"):
        return False
    state = task.get("state")
    if state in _STARTED_STATES:
        return True
    if state == "needs_human":
        return bool(task.get("pr_url"))
    if state == "waiting_agy":
        return any(e.get("kind") == "agy-status" for e in round_evs)
    return False


def queue(
    tasks: list[dict],
    events: dict[str, list[dict]],
    open_pr_files: dict[str, dict[str, set[str]]] | dict[str, list[set[str]]],
    issue_labels: dict[str, set[str]] | None = None,
) -> list[dict]:
    """Pure computation of the ordered agy-handoff work queue.

    Returns rows sorted ready-first, then by issue number, then by created time.
    """
    round_evs_by_id: dict[str, list[dict]] = {}
    files_by_id: dict[str, set[str]] = {}
    issue_by_id: dict[str, int | None] = {}
    for t in tasks:
        if t.get("lane") != "agy-handoff":
            continue
        tid = t["id"]
        evs = events.get(tid) or []
        round_evs_by_id[tid] = _round_events(evs)
        files_by_id[tid] = _files_from_events(evs)
        issue_by_id[tid] = issue_and_deps(t.get("brief"))[0]

    started_by_repo: dict[str | None, list[tuple[str, str, set[str]]]] = {}
    for t in tasks:
        if t.get("lane") != "agy-handoff":
            continue
        tid = t["id"]
        if not _is_started(t, round_evs_by_id[tid]):
            continue
        s_files = files_by_id[tid]
        if not s_files:
            continue
        s_issue = issue_by_id[tid]
        label = f"#{s_issue}" if s_issue is not None else tid
        started_by_repo.setdefault(t.get("repo"), []).append((tid, label, s_files))

    rows_with_key: list[tuple[tuple, dict]] = []
    for t in tasks:
        if t.get("lane") != "agy-handoff":
            continue
        tid = t["id"]
        if not _is_candidate(t, round_evs_by_id[tid]):
            continue
        repo = t.get("repo")
        cand_files = files_by_id[tid]
        blockers: set[str] = set()
        if cand_files:
            for other_id, label, s_files in started_by_repo.get(repo, ()):
                if other_id != tid and (cand_files & s_files):
                    blockers.add(label)
            repo_prs = (open_pr_files or {}).get(repo)
            if isinstance(repo_prs, dict):
                for pr_id, pr_files in repo_prs.items():
                    if pr_files and cand_files.intersection(pr_files):
                        blockers.add(f"PR#{pr_id}" if isinstance(pr_id, int) else str(pr_id))
            elif isinstance(repo_prs, (list, tuple)):
                for idx, pr_files in enumerate(repo_prs, 1):
                    if pr_files and cand_files.intersection(pr_files):
                        blockers.add(f"PR#{idx}")

        blocked_by = sorted(blockers)
        status = "blocked" if blocked_by else "ready"
        issue = issue_by_id[tid]
        labels = (issue_labels or {}).get(tid)
        if labels is None:
            labels = t.get("labels")
        model = suggest_model(labels)
        cmd = f"agy-next {tid} --model {model}" if model else f"agy-next {tid}"
        row = {
            "id": tid,
            "repo": repo,
            "issue": issue,
            "title": _title_from_task(t),
            "files": sorted(cand_files),
            "status": status,
            "blocked_by": blocked_by,
            "model": model,
            "cmd": cmd,
        }
        sort_key = (status != "ready", issue is None, issue or 0, t.get("created") or 0)
        rows_with_key.append((sort_key, row))

    rows_with_key.sort(key=lambda item: item[0])
    return [row for _, row in rows_with_key]


def _gh(args: list[str], timeout: float = _GH_TIMEOUT_S) -> tuple[int, str]:
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            print(f"agy_handoff: gh {' '.join(args[:2])} exited {r.returncode}")
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError) as e:
        print(f"agy_handoff: gh {' '.join(args[:2])} failed: {type(e).__name__}")
        return 1, ""


def _cache_put(cache: dict, key, value, max_size: int) -> None:
    if key in cache:
        del cache[key]
    elif len(cache) >= max_size:
        cache.pop(next(iter(cache)))
    cache[key] = value


def _pr_files(slug: str, number: int, head_oid: str) -> set[str]:
    key = (slug, number, head_oid)
    if head_oid and key in _PR_FILES_CACHE:
        return set(_PR_FILES_CACHE[key])
    try:
        rc, out = _gh(["pr", "diff", str(number), "-R", slug, "--name-only"])
    except Exception as e:  # noqa: BLE001
        print(f"agy_handoff: gh pr diff failed: {type(e).__name__}")
        return set()
    if rc != 0:
        return set()
    files = {ln.strip() for ln in out.splitlines() if ln.strip()}
    if head_oid:
        _cache_put(_PR_FILES_CACHE, key, files, _PR_FILES_CACHE_MAX)
    return set(files)


def _issue_labels(slug: str, issue: int) -> set[str]:
    now = time.time()
    key = (slug, issue)
    hit = _ISSUE_LABEL_CACHE.get(key)
    if hit is not None and now - hit[0] < _ISSUE_LABEL_TTL_S:
        return set(hit[1])
    try:
        rc, out = _gh(["issue", "view", str(issue), "-R", slug, "--json", "labels"])
    except Exception as e:  # noqa: BLE001
        print(f"agy_handoff: gh issue view failed: {type(e).__name__}")
        return set()
    if rc != 0:
        return set()
    try:
        data = json.loads(out or "{}")
    except ValueError:
        print(f"agy_handoff: invalid JSON from gh issue view {slug}#{issue}")
        return set()
    raw = data.get("labels") if isinstance(data, dict) else None
    labels: set[str] = set()
    if isinstance(raw, list):
        for lb in raw:
            if isinstance(lb, dict) and isinstance(lb.get("name"), str):
                labels.add(lb["name"])
            elif isinstance(lb, str):
                labels.add(lb)
    _cache_put(_ISSUE_LABEL_CACHE, key, (now, labels), _ISSUE_LABEL_CACHE_MAX)
    return set(labels)


def queue_inputs() -> tuple[
    list[dict],
    dict[str, list[dict]],
    dict[str, dict[str, set[str]]],
    dict[str, set[str]],
]:
    """Collect store tasks/events and best-effort GitHub PR/label data for `queue()`."""
    tasks = store.list_tasks()
    events: dict[str, list[dict]] = {}
    candidates: list[dict] = []
    tasks_by_id: dict[str, dict] = {}

    for t in tasks:
        tasks_by_id[t["id"]] = t
        if t.get("lane") != "agy-handoff":
            continue
        evs = store.events(t["id"])
        events[t["id"]] = evs
        if _is_candidate(t, _round_events(evs)):
            candidates.append(t)

    open_pr_files: dict[str, dict[str, set[str]]] = {}
    issue_labels: dict[str, set[str]] = {}
    if not candidates:
        return tasks, events, open_pr_files, issue_labels

    candidate_repos = {t.get("repo") or "" for t in candidates} - {""}
    repo_paths: dict[str, str] = {}
    for t in tasks:
        repo = t.get("repo") or ""
        if repo in candidate_repos and t.get("repo_path") and repo not in repo_paths:
            repo_paths[repo] = t["repo_path"]

    import ci_triage  # lazy: matches intake.py / loop.py

    slug_by_repo: dict[str, str] = {}
    for repo, repo_path in repo_paths.items():
        try:
            slug = ci_triage.repo_slug(repo_path)
        except Exception as e:  # noqa: BLE001
            print(f"agy_handoff: repo_slug failed for {repo}: {type(e).__name__}")
            slug = None
        if not slug:
            continue
        slug_by_repo[repo] = slug

        try:
            rc, out = _gh([
                "pr", "list", "-R", slug, "--state", "open",
                "--json", "number,headRefName,headRefOid,url",
            ])
        except Exception as e:  # noqa: BLE001
            print(f"agy_handoff: gh pr list failed: {type(e).__name__}")
            rc, out = 1, ""
        if rc == 0:
            try:
                prs = json.loads(out or "[]")
            except ValueError:
                print(f"agy_handoff: invalid JSON from gh pr list {slug}")
                prs = []
            if isinstance(prs, list):
                repo_prs: dict[str, set[str]] = {}
                for pr in prs:
                    if not isinstance(pr, dict):
                        continue
                    head = pr.get("headRefName") or ""
                    if not isinstance(head, str) or not head.startswith(("coxd/", "coxd-agy/")):
                        continue
                    num = pr.get("number")
                    if isinstance(num, bool) or not isinstance(num, int):
                        continue
                    oid = pr.get("headRefOid")
                    head_oid = oid if isinstance(oid, str) else ""
                    files = _pr_files(slug, num, head_oid)
                    tid = head.split("/", 1)[1]
                    pr_task = tasks_by_id.get(tid)
                    pr_issue = issue_and_deps(pr_task.get("brief"))[0] if pr_task else None
                    pr_key = f"#{pr_issue}" if pr_issue is not None else f"PR#{num}"
                    if files:
                        repo_prs.setdefault(pr_key, set()).update(files)
                if repo_prs:
                    open_pr_files[repo] = repo_prs

    for t in candidates:
        repo = t.get("repo") or ""
        slug = slug_by_repo.get(repo)
        if not slug:
            continue
        issue = issue_and_deps(t.get("brief"))[0]
        if issue is not None:
            issue_labels[t["id"]] = _issue_labels(slug, issue)

    return tasks, events, open_pr_files, issue_labels


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

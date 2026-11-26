"""agy handoff lane 3/4: pick up the human-pushed `coxd-agy/<id>` branch.

Plain git only (ls-remote / fetch) plus store bookkeeping. coxd never starts,
executes, signals, parses, or wraps agy; the only interface is the git branch
and the board API.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import notify
import registry
import sandbox
import store

BRANCH_PREFIX = "coxd-agy/"
AGY_PICKUP = "agy-pickup"  # queued reason: a new head was found on the agy branch
AGY_REPLAN = "agy-replan"  # queued reason: planned files changed on main; plan again
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


DEFAULT_PING_DEBOUNCE_S = 600.0
_PR_URL_NUM_RE = re.compile(r"/pull/(\d+)(?:[/?#]|$)")


def _ping_debounce_s() -> float:
    raw = os.environ.get("COXD_AGY_PING_DEBOUNCE_S")
    if raw is None or not raw.strip():
        return DEFAULT_PING_DEBOUNCE_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_PING_DEBOUNCE_S


def _notify_state_path() -> Path:
    return registry.home() / "agy_notify_state.json"


def _empty_notify_state() -> dict:
    return {
        "ready": [],
        "ready_rounds": {},
        "blocked": {},
        "merged": [],
        "prs_ready_sig": [],
        "pending": {},
    }


def _load_notify_state() -> dict:
    p = _notify_state_path()
    empty = _empty_notify_state()
    if not p.exists():
        return empty
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(raw, dict):
        return empty
    ready = raw.get("ready")
    ready_rounds = raw.get("ready_rounds")
    blocked = raw.get("blocked")
    merged = raw.get("merged")
    prs_ready_sig = raw.get("prs_ready_sig")
    pending = raw.get("pending")
    return {
        "ready": sorted(x for x in ready if isinstance(x, str)) if isinstance(ready, list) else [],
        "ready_rounds": (
            {
                k: int(v)
                for k, v in ready_rounds.items()
                if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
            }
            if isinstance(ready_rounds, dict)
            else {}
        ),
        "blocked": (
            {
                k: [b for b in v if isinstance(b, str)]
                for k, v in blocked.items()
                if isinstance(k, str) and isinstance(v, list)
            }
            if isinstance(blocked, dict)
            else {}
        ),
        "merged": [x for x in merged if isinstance(x, str)] if isinstance(merged, list) else [],
        "prs_ready_sig": (
            [x for x in prs_ready_sig if isinstance(x, str)]
            if isinstance(prs_ready_sig, list)
            else []
        ),
        "pending": pending if isinstance(pending, dict) else {},
    }


def _save_notify_state(state: dict) -> None:
    p = _notify_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=p.parent,
            prefix=f".{p.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_path = fh.name
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.flush()
        os.replace(tmp_path, p)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _plan_round(evs: list[dict]) -> int:
    return sum(1 for e in evs if e.get("kind") == "plan")


def _has_queue_ready(evs: list[dict], rnd: int) -> bool:
    return any(
        e.get("kind") == "queue-ready"
        and isinstance(e.get("data"), dict)
        and e["data"].get("round") == rnd
        for e in evs
    )


def _pr_files_set(pr: dict) -> set[str]:
    raw = pr.get("files")
    if raw is not None:
        return {f for f in raw if isinstance(f, str) and f}
    return plan_files(pr)


def _pr_label(task: dict) -> str:
    url = task.get("pr_url") or ""
    m = _PR_URL_NUM_RE.search(url)
    if m:
        return f"#{m.group(1)}"
    issue = issue_and_deps(task.get("brief"))[0]
    if issue is not None:
        return f"#{issue}"
    return str(task.get("id") or "")


def merge_order(prs: list[dict]) -> list[dict]:
    """Order ready-to-merge PRs oldest-first, ensuring any PR whose files overlap an
    older PR is placed after its last overlapping predecessor."""
    ordered = sorted(prs, key=lambda p: (p.get("created") is None, p.get("created") or 0))
    files_list = [_pr_files_set(p) for p in ordered]
    n = len(ordered)
    for i in range(n):
        f_i = files_list[i]
        if not f_i:
            continue
        c_i = ordered[i].get("created")
        last_pred = -1
        for j in range(n):
            if j == i:
                continue
            c_j = ordered[j].get("created")
            is_older = c_j is not None and c_i is not None and c_j < c_i
            if is_older and (f_i & files_list[j]):
                last_pred = max(last_pred, j)
        if last_pred > i:
            pr = ordered.pop(i)
            fl = files_list.pop(i)
            ordered.insert(last_pred, pr)
            files_list.insert(last_pred, fl)
    return ordered


def _present_blockers(
    tasks: list[dict],
    events: dict[str, list[dict]],
    open_pr_files: dict[str, dict[str, set[str]]] | dict[str, list[set[str]]],
) -> dict[str | None, set[str]]:
    present: dict[str | None, set[str]] = {}
    for t in tasks:
        if t.get("lane") != "agy-handoff":
            continue
        tid = t["id"]
        evs = events.get(tid) or []
        if not _is_started(t, _round_events(evs)):
            continue
        issue = issue_and_deps(t.get("brief"))[0]
        label = f"#{issue}" if issue is not None else tid
        present.setdefault(t.get("repo"), set()).add(label)
    for repo, repo_prs in (open_pr_files or {}).items():
        bucket = present.setdefault(repo, set())
        if isinstance(repo_prs, dict):
            for pr_id in repo_prs:
                bucket.add(f"PR#{pr_id}" if isinstance(pr_id, int) else str(pr_id))
        elif isinstance(repo_prs, (list, tuple)):
            for idx in range(1, len(repo_prs) + 1):
                bucket.add(f"PR#{idx}")
    return present


def notify_queue(now: float | None = None) -> list[str]:
    """Debounced, deduplicated queue and merge-order notifications for agy-handoff.

    Uses only the local store and GitHub CLI data via `queue_inputs()`; never starts,
    signals, or parses `agy`. Returns the list of notification channels fired.
    """
    ts = time.time() if now is None else float(now)
    debounce_s = _ping_debounce_s()
    state = _load_notify_state()

    tasks, events, open_pr_files, issue_labels = queue_inputs()
    rows = queue(tasks, events, open_pr_files, issue_labels)
    ready_rows = [r for r in rows if r["status"] == "ready"]

    # Tasks that just unblocked get a fresh base (or a re-plan) before we announce them.
    by_id = {t["id"]: t for t in tasks}
    changed = False
    for r in ready_rows:
        t = by_id.get(r["id"])
        if t is None or not state["blocked"].get(r["id"]):
            continue
        try:
            changed = refresh_unblocked(t) in ("refreshed", "replan") or changed
        except Exception as e:  # noqa: BLE001 - never break the queue pass
            print(f"agy_handoff: refresh failed for {r['id']}: {type(e).__name__}")
    if changed:
        tasks, events, open_pr_files, issue_labels = queue_inputs()
        rows = queue(tasks, events, open_pr_files, issue_labels)
        ready_rows = [r for r in rows if r["status"] == "ready"]
    blocked_rows = [r for r in rows if r["status"] == "blocked"]

    prev_blocked: dict[str, list[str]] = state["blocked"]
    current_blocked: dict[str, list[str]] = {r["id"]: list(r["blocked_by"]) for r in blocked_rows}
    present = _present_blockers(tasks, events, open_pr_files)

    ready_rounds: dict[str, int] = dict(state["ready_rounds"])
    merged_set: set[str] = set(state["merged"])
    pending: dict = dict(state["pending"])
    raw_pending_ready = pending.get("ready")
    pending_ready: dict[str, dict] = {}
    if isinstance(raw_pending_ready, dict):
        for tid, entry in raw_pending_ready.items():
            if (
                isinstance(tid, str)
                and isinstance(entry, dict)
                and isinstance(entry.get("round"), int)
                and not isinstance(entry.get("round"), bool)
                and isinstance(entry.get("ts"), (int, float))
                and not isinstance(entry.get("ts"), bool)
            ):
                unblocked = entry.get("unblocked_by")
                pending_ready[tid] = {
                    "round": entry["round"],
                    "ts": float(entry["ts"]),
                    "unblocked_by": (
                        [b for b in unblocked if isinstance(b, str)]
                        if isinstance(unblocked, list)
                        else []
                    ),
                    "cmd": entry.get("cmd") if isinstance(entry.get("cmd"), str) else "",
                }

    current_rounds: dict[str, int] = {}
    ready_ids: set[str] = set()
    for r in ready_rows:
        tid = r["id"]
        ready_ids.add(tid)
        evs = events.get(tid) or []
        rnd = _plan_round(evs)
        current_rounds[tid] = rnd
        if ready_rounds.get(tid) == rnd or _has_queue_ready(evs, rnd):
            ready_rounds[tid] = rnd
            pending_ready.pop(tid, None)
            continue
        existing = pending_ready.get(tid)
        if existing is None or existing["round"] != rnd:
            prev_b = prev_blocked.get(tid) or []
            repo_present = present.get(r.get("repo"), set())
            gone = [b for b in prev_b if b not in repo_present]
            pending_ready[tid] = {
                "round": rnd,
                "ts": ts,
                "unblocked_by": gone,
                "cmd": r["cmd"],
            }
        else:
            existing["cmd"] = r["cmd"]

    # Prune pending items that are no longer ready in the current queue.
    for tid in list(pending_ready):
        if tid not in ready_ids:
            del pending_ready[tid]

    sent: list[str] = []

    if pending_ready:
        oldest_ts = min(e["ts"] for e in pending_ready.values())
        if ts - oldest_ts >= debounce_s:
            to_announce = [r for r in ready_rows if r["id"] in pending_ready]
            if to_announce:
                first_cmd = to_announce[0]["cmd"]
                unblock_groups: dict[str, list[str]] = {}
                plain_cmds: list[str] = []
                for r in to_announce:
                    entry = pending_ready[r["id"]]
                    gone = entry.get("unblocked_by") or []
                    if gone:
                        key = ", ".join(gone)
                        unblock_groups.setdefault(key, []).append(r["cmd"])
                    else:
                        plain_cmds.append(r["cmd"])
                lines = [
                    f"{blocker} merged -> ready: {', '.join(cmds)}"
                    for blocker, cmds in unblock_groups.items()
                ]
                if plain_cmds:
                    lines.append(f"ready: {', '.join(plain_cmds)}")
                msg = "\n".join(lines)
                try:
                    ch = notify.notify("coxd · agy queue", msg, copy_text=first_cmd)
                except Exception as e:  # noqa: BLE001
                    print(f"agy_handoff: notify_queue ready send failed: {type(e).__name__}")
                else:
                    if isinstance(ch, list):
                        sent.extend(ch)
                    for r in to_announce:
                        tid = r["id"]
                        entry = pending_ready[tid]
                        rnd = entry["round"]
                        if not _has_queue_ready(store.events(tid), rnd):
                            store.append_event(tid, "queue-ready", {"round": rnd})
                        ready_rounds[tid] = rnd
                        merged_set.update(entry.get("unblocked_by") or [])
                    pending_ready.clear()

    # Ready-to-merge message for agy-handoff PRs.
    pr_tasks = [
        {
            **t,
            "files": (
                _files_from_events(events[t["id"]]) if t["id"] in events else plan_files(t)
            ),
        }
        for t in tasks
        if t.get("lane") == "agy-handoff"
        and t.get("state") == "pr_ready"
        and not t.get("archived_at")
        and t.get("pr_url")
    ]
    ordered_prs = merge_order(pr_tasks)
    current_sig = [_pr_label(p) for p in ordered_prs]
    raw_pending_merge = pending.get("prs_ready")
    pending_merge: dict | None = (
        dict(raw_pending_merge)
        if isinstance(raw_pending_merge, dict)
        and isinstance(raw_pending_merge.get("ts"), (int, float))
        and not isinstance(raw_pending_merge.get("ts"), bool)
        else None
    )

    if not current_sig:
        state["prs_ready_sig"] = []
        pending_merge = None
    elif current_sig == state["prs_ready_sig"]:
        pending_merge = None
    else:
        if pending_merge is None:
            pending_merge = {"ts": ts, "sig": list(current_sig)}
        else:
            pending_merge = {"ts": float(pending_merge["ts"]), "sig": list(current_sig)}
        if ts - pending_merge["ts"] >= debounce_s:
            merge_msg = (
                f"{len(current_sig)} PRs ready to merge, in this order: "
                f"{', '.join(current_sig)}"
            )
            try:
                ch = notify.notify("coxd · agy queue", merge_msg)
            except Exception as e:  # noqa: BLE001
                print(f"agy_handoff: notify_queue merge send failed: {type(e).__name__}")
            else:
                if isinstance(ch, list):
                    sent.extend(ch)
                state["prs_ready_sig"] = list(current_sig)
                pending_merge = None

    new_pending: dict = {}
    if pending_ready:
        new_pending["ready"] = pending_ready
    if pending_merge:
        new_pending["prs_ready"] = pending_merge

    state["ready"] = sorted(
        tid for tid, rnd in ready_rounds.items() if current_rounds.get(tid, rnd) == rnd
    )
    state["ready_rounds"] = ready_rounds
    state["blocked"] = current_blocked
    state["merged"] = sorted(merged_set)
    state["pending"] = new_pending
    _save_notify_state(state)
    return sent


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


def agy_tokens_trailer(worktree: str | Path, sha: str) -> int | None:
    """Read a non-negative integer `Agy-Tokens` git trailer from commit `sha` in
    `worktree`. Plain git only; returns None on error or when missing/malformed."""
    if not worktree or not sha:
        return None
    try:
        r = _git(Path(worktree), "log", "-1",
                 "--format=%(trailers:key=Agy-Tokens,valueonly)", sha)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None
    raw = lines[-1]
    if not re.fullmatch(r"[0-9]+", raw):
        return None
    return int(raw)


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


def replan_pending(task_id: str) -> bool:
    """True while an `agy-replan` event is newer than the latest `plan` event. The
    supervisor clears the queued reason on claim, so run_task reads this marker."""
    pending = False
    for e in store.events(task_id):
        if e.get("kind") == AGY_REPLAN:
            pending = True
        elif e.get("kind") == "plan":
            pending = False
    return pending


def default_branch_sha(repo_path: str) -> str | None:
    """Fetch origin in the main checkout and return the SHA of its default branch
    (origin/HEAD, falling back to origin/main), or None on any failure."""
    cwd = Path(repo_path)
    try:
        if _git(cwd, "fetch", "origin").returncode != 0:
            return None
        sym = _git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        ref = sym.stdout.strip() if sym.returncode == 0 and sym.stdout.strip() else "origin/main"
        r = _git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except (OSError, subprocess.SubprocessError):
        return None
    sha = r.stdout.strip()
    return sha if r.returncode == 0 and _SHA.match(sha) else None


def refresh_unblocked(task: dict) -> str:
    """A blocked agy task just became ready: bring its branch up to date with main.

    Returns "refreshed" (untouched branch re-pointed at main, base_sha updated, plan
    kept), "replan" (a planned file changed on main, so queue a new plan round),
    "skipped" (nothing to do, or the branch carries human commits), or "error".
    Plain git only; agy is never run.
    """
    tid = task.get("id")
    repo_path, base = task.get("repo_path"), task.get("base_sha")
    if (not tid or task.get("lane") != "agy-handoff" or task.get("state") != "waiting_agy"
            or task.get("archived_at") or not repo_path or not base):
        return "skipped"
    evs = store.events(tid)
    if any(e.get("kind") == "agy-head" for e in _round_events(evs)):
        return "skipped"  # agy/human already pushed in this round
    remote = remote_head(repo_path, tid)
    if remote != base:
        return "skipped"  # human commits (or the branch is gone): leave it alone
    main_sha = default_branch_sha(repo_path)
    if not main_sha or main_sha == base:
        return "skipped"
    cwd = Path(repo_path)
    try:
        diff = _git(cwd, "diff", "--name-only", base, main_sha)
    except (OSError, subprocess.SubprocessError) as e:
        store.append_event(tid, "agy-error", {"stage": "refresh", "err": type(e).__name__})
        return "error"
    if diff.returncode != 0:
        store.append_event(tid, "agy-error", {"stage": "refresh", "err": diff.stderr[-300:]})
        return "error"
    changed = {ln.strip() for ln in diff.stdout.splitlines() if ln.strip()}
    overlap = sorted(_files_from_events(evs) & changed)
    if overlap:
        if store.cas_state(tid, "waiting_agy", "queued", AGY_REPLAN):
            store.append_event(tid, AGY_REPLAN, {"from": base, "to": main_sha, "files": overlap})
            return "replan"
        return "skipped"
    branch = f"{BRANCH_PREFIX}{tid}"
    try:
        push = _git(cwd, "push", f"--force-with-lease=refs/heads/{branch}:{base}", "origin",
                    f"{main_sha}:refs/heads/{branch}")
    except (OSError, subprocess.SubprocessError) as e:
        store.append_event(tid, "agy-error", {"stage": "refresh", "err": type(e).__name__})
        return "error"
    if push.returncode != 0:
        store.append_event(tid, "agy-error", {"stage": "refresh", "err": push.stderr[-300:]})
        return "error"
    store.set_plan(tid, task.get("plan_md") or "", main_sha)
    store.append_event(tid, "base-refreshed", {"old": base, "new": main_sha, "branch": branch})
    return "refreshed"


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

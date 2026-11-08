"""Auto-rebase: after a coxd PR merges, bring the other open coxd PRs of that repo
up to date with the target branch (control plane, not the worker).

Per behind `pr_ready` task: ownership check (every commit must carry coxd's own
identity) -> `git rebase origin/<target>` -> clean: re-run gate + PII gate, then
`--force-with-lease` push of the task's own branch -> conflict: abort and hand the
task to the existing resume-from-pr_ready path with a conflict note. At most one
attempt per task per target-branch commit (recorded in the event's `target_sha`).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import ci_triage
import envscrub
import factory
import gate
import gitident
import quota
import registry
import sandbox
import ship
import store

EVENTS = ("rebased", "rebase-skipped", "rebase-conflict", "rebase-failed")
CONFLICT_NOTE = ("Your PR branch no longer rebases cleanly onto the latest target branch "
                 "`{target}` (it moved on after another PR merged). MERGE the latest "
                 "`origin/{target}` into your branch (do NOT rebase or rewrite history — "
                 "the push is a plain fast-forward), resolve the conflicts, re-run the "
                 "checks and commit. Do not push.")

NO_IDENTITY = "no identity"

_lock = threading.Lock()


def _pending_file() -> Path:
    return registry.home() / "rebase_pending.json"


def _load_pending() -> set[str]:
    try:
        return set(json.loads(_pending_file().read_text()))
    except (OSError, ValueError, TypeError):
        return set()


def _save_pending(repos: set[str]) -> None:
    try:
        _pending_file().write_text(json.dumps(sorted(repos)))
    except OSError:
        pass


def mark_merged(repo: str) -> None:
    """A merge was just detected in `repo`: queue a rebase sweep for it."""
    with _lock:
        _save_pending(_load_pending() | {repo})


def _git(cwd, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env=env)


def _out(cwd, *args: str) -> str | None:
    r = _git(cwd, *args)
    return r.stdout.strip() if r.returncode == 0 else None


def identity(repo_path: Path) -> tuple[str, str] | None:
    """coxd's own commit identity (name, email); see gitident for the lookup order.
    None → unknown (fail closed: nothing is rebased)."""
    return gitident.identity(repo_path)


def _foreign_commits(repo_path: Path, base: str, branch: str, email: str) -> bool:
    """True if any commit in base..branch has an author or committer other than us."""
    log = _out(repo_path, "log", "--format=%ae%x00%ce", f"{base}..refs/heads/{branch}")
    if log is None:
        return True  # can't tell → treat as foreign
    for line in log.splitlines():
        for e in line.split("\0"):
            if e.strip().lower() != email.strip().lower():
                return True
    return False


def _done_for(tid: str, sha: str) -> bool:
    return any(e["kind"] in EVENTS and e["data"].get("target_sha") == sha
               and e["data"].get("reason") != NO_IDENTITY  # transient: retry
               for e in store.events(tid))


def _pr_open(pr_url: str, slug: str) -> bool:
    n = pr_url.rstrip("/").rsplit("/", 1)[-1]
    r = subprocess.run(["gh", "pr", "view", n, "-R", slug, "--json", "state"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    try:
        return json.loads(r.stdout).get("state") == "OPEN"
    except ValueError:
        return False


def _blocked(running, concurrency: int) -> bool:
    """`running` is an int or a callable returning the live count (re-read per task)."""
    n = running() if callable(running) else running
    return factory.is_paused() or quota.blocked_until() is not None \
        or n >= concurrency


def _wt_git(wt: Path, args: list[str], ident: dict) -> subprocess.CompletedProcess:
    """git inside the worker-owned worktree: as the worker user (sandbox.wrap), with a
    scrubbed env (no GH_TOKEN/TG token) plus coxd's commit identity. Never as root."""
    return subprocess.run(sandbox.wrap(["git", *args]), cwd=str(wt), capture_output=True,
                          text=True, env=envscrub.scrubbed_env(ident))


def _reset(wt: Path, orig: str, env: dict) -> None:
    """Undo a local rebase that was not pushed, so the local branch matches the remote PR
    branch again (the next ship push stays a fast-forward)."""
    _wt_git(wt, ["reset", "--hard", orig], env)


def rebase_task(t: dict, slug: str, target: str, sha: str, who: tuple[str, str]) -> str:
    """One attempt for one task. Returns the outcome (also recorded as an event)."""
    tid = t["id"]
    repo_path = Path(t["repo_path"])
    wt = Path(t["worktree"])
    branch = f"coxd/{tid}"
    base = f"origin/{target}"
    name, email = who

    def rec(kind: str, **data) -> str:
        store.append_event(tid, kind, {"target_sha": sha, "target": target, **data})
        return kind

    if branch == target or not wt.is_dir():
        return rec("rebase-skipped", reason="no-worktree")
    if _foreign_commits(repo_path, base, branch, email):
        return rec("rebase-skipped", reason="foreign commits")
    old_sha = _out(repo_path, "rev-parse", "--verify", f"refs/remotes/origin/{branch}")
    if not old_sha:
        return rec("rebase-skipped", reason="no remote branch")
    head = sandbox.run_in_worktree(["git", "symbolic-ref", "--short", "HEAD"], wt)
    if head.returncode != 0 or head.stdout.strip() != branch:
        return rec("rebase-skipped", reason="worktree not on task branch")

    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    orig = _out(repo_path, "rev-parse", "--verify", f"refs/heads/{branch}")
    if not orig:
        return rec("rebase-skipped", reason="no local branch")
    rb = _wt_git(wt, ["rebase", base], env)
    if rb.returncode != 0:
        _wt_git(wt, ["rebase", "--abort"], env)
        if not t.get("session_id"):
            return rec("rebase-skipped", reason="conflict, no session to resume")
        import board  # lazy: board pulls in the whole web stack
        rec("rebase-conflict")
        note = CONFLICT_NOTE.format(target=target)
        store.append_event(tid, "resume-request", {"note": note})
        store.set_state(tid, "queued", board.HUMAN_RESUME)
        return "rebase-conflict"

    entry = registry.load(t["repo"]) or {}
    g = gate.run_gate(wt, entry)
    if not g.get("passed"):
        _reset(wt, orig, env)
        rec("rebase-failed", stage="gate", failing=g.get("failing"))
        store.set_state(tid, "needs_human", "gate-red")
        return "gate-red"
    ctx: dict = {}
    reason = ship._pii_gate(tid, t, repo_path, branch, wt, ctx)
    if reason:
        _reset(wt, orig, env)
        rec("rebase-failed", stage="pii", reason=reason)
        store.set_state(tid, "needs_human", reason)
        return reason
    new_sha = ctx.get("sha") or _out(repo_path, "rev-parse", f"refs/heads/{branch}")
    push = subprocess.run(
        ["git", "push", f"--force-with-lease=refs/heads/{branch}:{old_sha}",
         "origin", f"{new_sha}:refs/heads/{branch}"],
        cwd=str(repo_path), capture_output=True, text=True)
    if push.returncode != 0:
        _reset(wt, orig, env)
        return rec("rebase-failed", stage="push", err=(push.stderr or "")[-300:])
    return rec("rebased", sha=new_sha)


def rebase_repo(repo: str, running=0, concurrency: int = 2) -> bool:
    """Sweep one repo's other pr_ready tasks, in order, one at a time.
    Returns False if deferred (paused / quota / concurrency) — retry on a later pass."""
    tasks = [t for t in store.list_tasks()
             if t["repo"] == repo and t["state"] == "pr_ready" and t["pr_url"]
             and t["repo_path"] and not store.is_archived(t)]
    if not tasks:
        return True
    if _blocked(running, concurrency):
        return False
    repo_path = Path(tasks[0]["repo_path"])
    slug = ci_triage.repo_slug(str(repo_path))
    target = (registry.load(repo) or {}).get("target_branch") or "main"
    if not slug:
        return True
    if _git(repo_path, "fetch", "--quiet", "--prune").returncode != 0:
        return False
    sha = _out(repo_path, "rev-parse", "--verify", f"refs/remotes/origin/{target}")
    if not sha:
        return True
    who = identity(repo_path)
    if not who:
        # Never silently drop the sweep: say so on each task (once per target sha)
        # and stay pending so it retries once an identity is configured.
        for t in tasks:
            if not any(e["kind"] == "rebase-skipped"
                       and e["data"].get("reason") == NO_IDENTITY
                       and e["data"].get("target_sha") == sha
                       for e in store.events(t["id"])):
                store.append_event(t["id"], "rebase-skipped",
                                   {"target_sha": sha, "target": target,
                                    "reason": NO_IDENTITY})
        return False
    for t in tasks:
        if _blocked(running, concurrency):
            return False
        cur = store.get_task(t["id"])
        if not cur or cur["state"] != "pr_ready" or _done_for(t["id"], sha):
            continue
        branch = f"coxd/{t['id']}"
        behind = _out(repo_path, "rev-list", "--count",
                      f"refs/heads/{branch}..origin/{target}")
        if not behind or behind == "0" or not _pr_open(t["pr_url"], slug):
            continue
        # Claim before touching the worktree (like the supervisor's 'fixing') so a
        # board /resume or the runner can't start a worker in it mid-rebase.
        if not store.cas_state(t["id"], "pr_ready", "gating", "auto-rebase"):
            continue  # a human resume / other writer got there first
        try:
            rebase_task(cur, slug, target, sha, who)
        except Exception as e:  # noqa: BLE001 - one task must not stop the sweep
            store.append_event(t["id"], "rebase-failed",
                               {"target_sha": sha, "stage": "error", "err": str(e)[-300:]})
        finally:
            store.cas_state(t["id"], "gating", "pr_ready", None,
                            expect_reason="auto-rebase")
    return True


def process_pending(running=0, concurrency: int = 2) -> None:
    with _lock:
        repos = sorted(_load_pending())
    for repo in repos:
        if rebase_repo(repo, running, concurrency):
            with _lock:
                _save_pending(_load_pending() - {repo})

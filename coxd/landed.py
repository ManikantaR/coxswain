"""Post-merge cleanup poller (DESIGN-V35 extension).

Every PR merge tonight needed the same three manual steps afterward: remove the
worktree, delete the local+remote coxd/<id> branch, close the issue if it didn't
auto-link. None of that requires human judgment — it's pure bookkeeping once a
human has already made the one real decision (the merge itself). Detect the merge
and do the bookkeeping automatically instead of repeating it by hand every time.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import ci_triage
import store
import worktree

_ISSUE_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)")


def _run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def _pr_merged(pr_url: str, repo_slug: str) -> tuple[bool, str | None]:
    pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]
    r = _run(["gh", "pr", "view", pr_number, "-R", repo_slug, "--json", "state,mergedAt"])
    if r.returncode != 0:
        return (False, None)
    import json
    try:
        d = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return (False, None)
    return (d.get("state") == "MERGED", d.get("mergedAt"))


def _close_issue_if_open(brief: str, repo_slug: str, pr_url: str) -> None:
    m = _ISSUE_URL_RE.search(brief or "")
    if not m or m.group(1) != repo_slug:
        return
    number = m.group(2)
    r = _run(["gh", "issue", "view", number, "-R", repo_slug, "--json", "state"])
    if r.returncode != 0 or '"OPEN"' not in r.stdout:
        return  # already closed (likely auto-linked), or lookup failed — leave it alone
    _run(["gh", "issue", "close", number, "-R", repo_slug,
         "--comment", f"Landed via {pr_url}."])


def check_and_cleanup_merged() -> None:
    """One pass: for every pr_ready task, check if its PR merged; if so, clean up."""
    for t in store.list_tasks():
        if t["state"] != "pr_ready" or not t["pr_url"] or not t["repo_path"]:
            continue
        slug = ci_triage.repo_slug(t["repo_path"])
        if not slug:
            continue
        merged, _ = _pr_merged(t["pr_url"], slug)
        if not merged:
            continue
        store.append_event(t["id"], "auto-cleanup", {"pr_url": t["pr_url"]})
        _close_issue_if_open(t["brief"], slug, t["pr_url"])
        branch = f"coxd/{t['id']}"
        worktree.remove(Path(t["repo_path"]), Path(t["worktree"]), branch)
        _run(["git", "push", "origin", "--delete", branch], cwd=t["repo_path"])
        pr_number = t["pr_url"].rstrip("/").rsplit("/", 1)[-1]
        store.set_state(t["id"], "landed", f"merged as PR #{pr_number}")

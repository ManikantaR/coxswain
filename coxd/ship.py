"""Push + open PR (DESIGN-V35; survives the pivot).

The control plane pushes and opens the PR — workers never can (the no-push hook).
If the repo has no GitHub remote (a local/scratch repo), this is a no-op and the
task is simply pr_ready-local. Never raises into the loop; a push/PR failure
becomes a typed needs-human reason the caller can route.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import store


def _has_gh_remote(repo_path: Path) -> bool:
    r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_path,
                       capture_output=True, text=True)
    return r.returncode == 0 and ("github.com" in r.stdout or "git@" in r.stdout)


def ship(task_id: str) -> tuple[str, str | None]:
    """Returns (outcome, pr_url). outcome: 'pr' | 'local' | 'push-error' | 'pr-error'."""
    t = store.get_task(task_id)
    wt = Path(t["worktree"])
    repo_path = Path(t["repo_path"]) if t["repo_path"] else None
    branch = f"coxd/{task_id}"

    if repo_path is None or not _has_gh_remote(repo_path):
        return ("local", None)  # nothing to push to — the work is on the local branch

    push = subprocess.run(["git", "push", "-u", "origin", branch], cwd=wt,
                          capture_output=True, text=True)
    if push.returncode != 0:
        store.append_event(task_id, "ship-error", {"stage": "push", "err": push.stderr[-300:]})
        return ("push-error", None)

    title = t["brief"].strip().splitlines()[0][:70]
    body = f"Dispatched via coxd (`{task_id}`).\n\nCost: ${t['cost'] or 0:.3f}\n\n🤖 coxd"
    pr = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body, "--head", branch],
        cwd=wt, capture_output=True, text=True)
    if pr.returncode != 0:
        store.append_event(task_id, "ship-error", {"stage": "pr", "err": pr.stderr[-300:]})
        return ("pr-error", None)
    url = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else None
    if url:
        store.set_pr_url(task_id, url)
    return ("pr", url)

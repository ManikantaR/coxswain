"""Disposable git worktrees per task (DESIGN-V35; survives the pivot).

One worktree + branch coxd/<id> off the repo's default branch, isolated from the
primary checkout. The SDK session runs with this as its cwd; the no-push hook +
the control plane holding creds keep workers from pushing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import registry


def _default_base(repo_path: Path) -> str:
    for ref in ("origin/HEAD", "origin/main", "main", "master"):
        r = subprocess.run(["git", "rev-parse", "--verify", "-q", ref],
                           cwd=repo_path, capture_output=True, text=True)
        if r.returncode == 0:
            return ref.split("/")[-1] if not ref.startswith("origin/") else ref
    return "HEAD"


def create(repo_path: Path, task_id: str) -> Path:
    wt = registry.home() / "worktrees" / task_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    branch = f"coxd/{task_id}"
    subprocess.run(["git", "fetch", "--quiet"], cwd=repo_path,
                   capture_output=True)  # best-effort; local scratch repos have no remote
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(wt), _default_base(repo_path)],
        cwd=repo_path, check=True, capture_output=True, text=True,
    )
    return wt


def remove(repo_path: Path, wt: Path, branch: str) -> None:
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                   cwd=repo_path, capture_output=True)
    subprocess.run(["git", "branch", "-D", branch], cwd=repo_path, capture_output=True)

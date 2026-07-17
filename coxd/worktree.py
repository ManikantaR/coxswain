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


def provision(wt: Path) -> dict:
    """Install deps so the worker + gate can run the repo's REAL checks.

    A fresh `git worktree` has no node_modules/venv, so a monorepo gate dies on
    `turbo: command not found` (the #98 shakedown bug). Detect the toolchain from
    the lockfiles carried into the worktree and install. Best-effort + deterministic:
    try the reproducible install first, fall back to a loose one, return a typed
    result the loop records. Zero tokens.
    """
    if (wt / "pnpm-lock.yaml").exists():
        plans = ["pnpm install --frozen-lockfile", "pnpm install"]
    elif (wt / "yarn.lock").exists():
        plans = ["yarn install --frozen-lockfile", "yarn install"]
    elif (wt / "package-lock.json").exists():
        plans = ["npm ci", "npm install"]
    elif (wt / "package.json").exists():
        plans = ["npm install"]
    else:
        return {"skipped": "no node manifest"}
    last: dict = {}
    for cmd in plans:
        r = subprocess.run(["sh", "-c", cmd], cwd=wt, capture_output=True, text=True)
        last = {"ran": cmd, "ok": r.returncode == 0}
        if r.returncode == 0:
            return last
        last["err"] = (r.stderr or r.stdout)[-400:]
    return last


def remove(repo_path: Path, wt: Path, branch: str) -> None:
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                   cwd=repo_path, capture_output=True)
    subprocess.run(["git", "branch", "-D", branch], cwd=repo_path, capture_output=True)

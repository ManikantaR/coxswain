"""Disposable git worktrees per task (DESIGN-V35; survives the pivot).

One worktree + branch coxd/<id> off the repo's default branch, isolated from the
primary checkout. The SDK session runs with this as its cwd; the no-push hook +
the control plane holding creds keep workers from pushing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import envscrub
import registry
import sandbox


def _default_base(repo_path: Path) -> str:
    for ref in ("origin/HEAD", "origin/main", "main", "master"):
        r = subprocess.run(["git", "rev-parse", "--verify", "-q", ref],
                           cwd=repo_path, capture_output=True, text=True)
        if r.returncode == 0:
            return ref.split("/")[-1] if not ref.startswith("origin/") else ref
    return "HEAD"


def _fetch(repo_path: Path) -> None:
    """Refresh origin before branching. Local scratch repos have no remote → skip.

    A repo WITH a remote whose fetch fails (e.g. no git credentials in the NAS
    container after a rebuild) used to be swallowed, so the worker silently branched
    from a weeks-old clone. Fail loudly instead — a stale base is worse than no task.
    """
    remotes = subprocess.run(["git", "remote"], cwd=repo_path,
                             capture_output=True, text=True).stdout.split()
    if not remotes:
        return
    r = subprocess.run(["git", "fetch", "--quiet", "--prune"], cwd=repo_path,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git fetch failed in {repo_path} — refusing to branch from a "
                           f"stale base: {(r.stderr or '').strip()[:300]}")


def root() -> Path:
    """Where task worktrees go. COXD_WORKTREES (the NAS sets /work) keeps them OUT of
    COXD_HOME, so a worker's relative paths (`../..`) can't land on the live store,
    registry or markers. Unset → the old <home>/worktrees (laptop default).
    Tasks keep the absolute path stored at dispatch, so moving this only affects new ones."""
    p = os.environ.get("COXD_WORKTREES")
    return Path(p) if p else registry.home() / "worktrees"


def create(repo_path: Path, task_id: str) -> Path:
    wt = root() / task_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    branch = f"coxd/{task_id}"
    _fetch(repo_path)
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(wt), _default_base(repo_path)],
        cwd=repo_path, check=True, capture_output=True, text=True,
    )
    # The worker owns its checkout and its per-worktree git admin dir (index, HEAD);
    # the shared objects/refs are group-writable (see deploy/entrypoint.sh).
    sandbox.own(wt, repo_path / ".git" / "worktrees" / task_id)
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
        # install runs package lifecycle scripts the worker may have edited → no secrets
        r = subprocess.run(sandbox.wrap(["sh", "-c", cmd]), cwd=wt, capture_output=True,
                           text=True, env=envscrub.scrubbed_env())
        last = {"ran": cmd, "ok": r.returncode == 0}
        if r.returncode == 0:
            return last
        last["err"] = (r.stderr or r.stdout)[-400:]
    return last


def remove(repo_path: Path, wt: Path, branch: str) -> None:
    # The worktree dir as the worker: it owns it, so root deleting a path the worker
    # controls (it could swap in a symlink) would be unsafe. The branch ref as root, in
    # the main checkout: deleting a ref rewrites .git/packed-refs, which is root-only.
    # Best-effort, like before.
    sandbox.run_in_worktree(["git", "worktree", "remove", "--force", str(wt)], repo_path)
    subprocess.run(["git", "branch", "-D", branch], cwd=repo_path, capture_output=True)

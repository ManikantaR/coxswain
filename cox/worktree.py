"""Disposable git worktrees per task (T-05, salvage: relay py/relay_spawn.py).

One worktree + branch `cox/<id>` per task, isolated from the primary checkout
(DESIGN §2.3). Unlike relay, EVERY git returncode is checked (relay swallowed
`git worktree add` failures, which then looked like a hang). No PTY/tmux (P7).
Teardown is fail-closed (T-12): refuses to remove a worktree whose work is not
provably landed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import home, proc


class UnlandedWorkError(RuntimeError):
    """Teardown refused: worktree has commits not provably on the remote/target."""


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str


def _default_branch(repo_path: Path) -> str:
    r = proc.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo_path,
        ok_rc=(0, 1, 128),
    )
    if r.rc == 0 and r.out.strip():
        return r.out.strip().split("/", 1)[-1]
    # Fall back to the checked-out branch of the primary repo.
    head = proc.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    return head.out.strip() or "main"


def create(repo_path: Path, task_id: str, base: str | None = None) -> Worktree:
    """Create worktrees/<id> on branch cox/<id> from base (default branch)."""
    repo_path = repo_path.expanduser().resolve()
    if not (repo_path / ".git").exists():
        raise proc.BosunProcError(["git", "-C", str(repo_path)], 128, "", "not a git repo")

    base_branch = base or _default_branch(repo_path)
    proc.run(["git", "fetch", "--quiet"], cwd=repo_path, ok_rc=(0, 1, 128))

    wt_path = home.worktrees_dir() / task_id
    branch = f"cox/{task_id}"
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    # Checked (relay swallowed this): a failure here raises instead of hanging.
    proc.run(
        ["git", "worktree", "add", "-b", branch, str(wt_path), base_branch],
        cwd=repo_path,
    )
    return Worktree(path=wt_path, branch=branch)


def _is_landed(repo_path: Path, worktree: Path, target_branch: str) -> bool:
    """True if the worktree HEAD is reachable from target OR its patch-id is
    contained in the target (squash-merge-safe, firstmate rule, T-12)."""
    head = proc.run(["git", "rev-parse", "HEAD"], cwd=worktree).out.strip()
    # Reachable directly?
    reach = proc.run(
        ["git", "merge-base", "--is-ancestor", head, f"origin/{target_branch}"],
        cwd=repo_path,
        ok_rc=(0, 1),
    )
    if reach.rc == 0:
        return True
    # `git cherry` marks with '-' the commits whose patch-id is already upstream.
    cherry = proc.run(
        ["git", "cherry", f"origin/{target_branch}", head],
        cwd=repo_path,
        ok_rc=(0, 1),
    )
    lines = [ln for ln in cherry.out.splitlines() if ln.strip()]
    return bool(lines) and all(ln.startswith("-") for ln in lines)


def remove(
    repo_path: Path,
    worktree: Worktree,
    target_branch: str,
    *,
    force: bool = False,
) -> None:
    """Fail-closed teardown (DESIGN §2.3, T-12). Refuses unless landed or forced."""
    repo_path = repo_path.expanduser().resolve()
    if not force and not _is_landed(repo_path, worktree.path, target_branch):
        head = proc.run(["git", "rev-parse", "--short", "HEAD"], cwd=worktree.path).out.strip()
        raise UnlandedWorkError(
            f"{worktree.branch}: HEAD {head} is not on origin/{target_branch} and no "
            f"patch-id match — refusing teardown. Use force=True to discard."
        )
    proc.run(
        ["git", "worktree", "remove", "--force", str(worktree.path)],
        cwd=repo_path,
        ok_rc=(0, 1, 128),
    )
    proc.run(["git", "branch", "-D", worktree.branch], cwd=repo_path, ok_rc=(0, 1, 128))


def unlanded_commits(repo_path: Path, worktree: Worktree, target_branch: str) -> list[str]:
    """Commits that would be lost by a force teardown (for the --force preview)."""
    r = proc.run(
        ["git", "log", "--oneline", f"origin/{target_branch}..HEAD"],
        cwd=worktree.path,
        ok_rc=(0, 1, 128),
    )
    return [ln for ln in r.out.splitlines() if ln.strip()]

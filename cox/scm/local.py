"""Local-bare-repo SCM for tokenless e2e tests (T-13).

Stands in for GitHub: pushes to a local bare remote, "opens a PR" by recording
a ref, and "merges" by fast-forwarding the target branch in the bare repo. No
network, no gh.
"""

from __future__ import annotations

from pathlib import Path

from .. import proc


class LocalScm:
    name = "local"

    def push(self, worktree: Path, branch: str) -> None:
        proc.run(["git", "push", "-u", "origin", branch], cwd=worktree)

    def create_pr(self, repo: Path, branch: str, title: str, body: str, target: str) -> str:
        # No PR service locally; the "URL" is a synthetic ref the test can assert on.
        return f"local://{branch}"

    def pr_checks_green(self, repo: Path, pr_url: str) -> bool | None:
        return True

    def merge(self, repo: Path, pr_url: str, squash: bool = True) -> None:
        branch = pr_url.removeprefix("local://")
        # Merge the branch into target inside the working repo, then push target.
        target = proc.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).out.strip()
        proc.run(["git", "merge", "--squash", branch], cwd=repo, ok_rc=(0, 1))
        proc.run(
            ["git", "-c", "user.email=cox@test", "-c", "user.name=cox",
             "commit", "-q", "-m", f"merge {branch}"],
            cwd=repo, ok_rc=(0, 1),
        )  # fmt: skip
        proc.run(["git", "push", "origin", target], cwd=repo, ok_rc=(0, 1))

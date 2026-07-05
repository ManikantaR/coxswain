"""SCM interface (T-11). github (v0) + local (tests); azdevops/tfs land in V1.

The trust boundary lives here: only the control plane pushes and opens PRs,
never the worker (DESIGN P6). Failures are typed so needs-human carries a real
reason, not a generic bucket.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class PushRejected(RuntimeError):
    pass


class PrError(RuntimeError):
    pass


class Scm(Protocol):
    name: str

    def push(self, worktree: Path, branch: str) -> None: ...
    def create_pr(self, repo: Path, branch: str, title: str, body: str, target: str) -> str: ...
    def pr_checks_green(self, repo: Path, pr_url: str) -> bool | None: ...
    def merge(self, repo: Path, pr_url: str, squash: bool = True) -> None: ...


def get_scm(name: str) -> Scm:
    if name == "github":
        from .github import GitHubScm

        return GitHubScm()
    if name == "local":
        from .local import LocalScm

        return LocalScm()
    raise ValueError(f"unknown scm {name!r} (available: github, local)")

"""GitHub SCM via `gh` (T-11, salvage: relay relay_control.py:223-270).

Worker never pushes; the control plane does (DESIGN P6). Every gh/git failure
maps to a typed error so needs-human carries push-rejected / pr-error.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import proc
from .base import PrError, PushRejected


class GitHubScm:
    name = "github"

    def push(self, worktree: Path, branch: str) -> None:
        try:
            proc.run(["git", "push", "-u", "origin", branch], cwd=worktree)
        except proc.BosunProcError as e:
            raise PushRejected(f"push {branch} rejected: {e.err.strip()}") from e

    def create_pr(self, repo: Path, branch: str, title: str, body: str, target: str) -> str:
        try:
            r = proc.run(
                ["gh", "pr", "create", "--head", branch, "--base", target,
                 "--title", title, "--body", body],
                cwd=repo,
            )  # fmt: skip
        except proc.BosunProcError as e:
            raise PrError(f"gh pr create failed: {e.err.strip()}") from e
        url = r.out.strip().splitlines()[-1] if r.out.strip() else ""
        if not url.startswith("http"):
            raise PrError(f"gh pr create returned no URL: {r.out!r}")
        return url

    def pr_checks_green(self, repo: Path, pr_url: str) -> bool | None:
        r = proc.run(
            ["gh", "pr", "view", pr_url, "--json", "statusCheckRollup,mergeable"],
            cwd=repo,
            ok_rc=(0, 1),
        )
        if r.rc != 0:
            return None
        data = json.loads(r.out)
        rollup = data.get("statusCheckRollup") or []
        if not rollup:
            return None  # no checks configured -> unknown, human decides
        states = {c.get("conclusion") or c.get("state") for c in rollup}
        if states & {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"}:
            return False
        if states & {"PENDING", "IN_PROGRESS", "QUEUED", None}:
            return None
        return True

    def merge(self, repo: Path, pr_url: str, squash: bool = True) -> None:
        flag = "--squash" if squash else "--merge"
        try:
            proc.run(["gh", "pr", "merge", pr_url, flag, "--delete-branch"], cwd=repo)
        except proc.BosunProcError as e:
            raise PrError(f"gh pr merge failed: {e.err.strip()}") from e

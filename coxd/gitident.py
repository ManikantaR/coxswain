"""coxd's own git commit identity, shared by auto-rebase and the clean-merge streak.

Per field, first hit wins: COXD_GIT_NAME/EMAIL -> GIT_AUTHOR_NAME/EMAIL -> the repo's
git config user.name/email."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _cfg(repo_path, key: str) -> str | None:
    if not repo_path:
        return None
    r = subprocess.run(["git", "-C", str(repo_path), "config", key],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _field(coxd_env: str, git_env: str, repo_path, key: str) -> str:
    for k in (coxd_env, git_env):
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    return _cfg(repo_path, key) or ""


def email(repo_path: str | Path | None) -> str:
    """Identity email, "" if unresolvable."""
    return _field("COXD_GIT_EMAIL", "GIT_AUTHOR_EMAIL", repo_path, "user.email")


def identity(repo_path: str | Path | None) -> tuple[str, str] | None:
    """(name, email), or None if either is unresolvable (fail closed)."""
    name = _field("COXD_GIT_NAME", "GIT_AUTHOR_NAME", repo_path, "user.name")
    em = email(repo_path)
    return (name, em) if name and em else None

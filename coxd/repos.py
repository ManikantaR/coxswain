"""Repo + issue helpers for dispatch-from-board (DESIGN-V35).

Lists the git repos under a clone-root (COXD_REPO_ROOT, default ~/repo) for the
board's repo picker, and pulls GitHub issues via `gh` so the captain dispatches
an issue URL/number instead of retyping the brief.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def repo_root() -> Path:
    return Path(os.environ.get("COXD_REPO_ROOT", str(Path.home() / "repo")))


def list_repos() -> list[dict]:
    root = repo_root()
    if not root.exists():
        return []
    return [{"name": c.name, "path": str(c)}
            for c in sorted(root.iterdir()) if c.is_dir() and (c / ".git").exists()]


def list_issues(repo_path: str) -> dict:
    if not repo_path:
        return {"error": "pick a repo first"}
    r = subprocess.run(
        ["gh", "issue", "list", "--json", "number,title,url", "--limit", "30"],
        cwd=repo_path, capture_output=True, text=True)
    if r.returncode != 0:
        return {"error": (r.stderr or "gh issue list failed").strip()[:200]}
    try:
        return {"issues": json.loads(r.stdout or "[]")}
    except json.JSONDecodeError:
        return {"error": "could not parse gh output"}


def resolve_issue(ref: str, repo_path: str | None) -> dict:
    ref = ref.strip()
    cwd = repo_path if (repo_path and "://" not in ref) else None
    r = subprocess.run(
        ["gh", "issue", "view", ref, "--json", "number,title,body,url"],
        cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        return {"error": (r.stderr or "gh issue view failed").strip()[:200]}
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"error": "could not parse gh output"}
    return {"title": d.get("title", ""), "body": d.get("body") or "", "url": d.get("url", "")}

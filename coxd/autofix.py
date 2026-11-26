"""Free pre-gate auto-fix: run the repo's formatters/linters in fix mode and commit the result.

Formatting/lint-only gate failures used to cost a Claude fix round. Running the repo's own
fixers ($0) before the gate means they never reach one. Opt-in per repo via the registry
`autofix` list (scout() seeds it for NEW repos only); an entry without the key runs nothing.

Safety: the fixers run exactly like the gate (worker user via sandbox.wrap, scrubbed env, a
deadline). Only already-TRACKED files are staged (`git add -u`), and only when the tree was
clean beforehand, so the commit holds nothing but fixer output. A failing fixer is
non-fatal (the gate decides red). If the result can't be committed it is reverted, so the
gate never goes green on uncommitted (never-pushed) changes.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import envscrub
import gate
import gitident
import registry
import sandbox

COMMIT_MSG = "chore: autofix (formatter/linter)"
TIMEOUT_S = 300


def _git(wt: Path, args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(sandbox.wrap(["git", *args]), cwd=str(wt), capture_output=True,
                          text=True, env=env)


def _lines(r: subprocess.CompletedProcess) -> list[str]:
    return [ln for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []


def run(worktree: Path, entry: dict, repo_path: str | Path | None = None) -> dict:
    """Run the fixers; commit their output. Returns {ran, committed, files, skipped?}."""
    cmds = registry.autofix_cmds(entry)
    out: dict = {"ran": [], "committed": False, "files": []}
    if not cmds:
        return out
    wt = Path(worktree)
    env = envscrub.scrubbed_env(entry.get("gate_env") or {})
    before = _git(wt, ["status", "--porcelain", "--untracked-files=no"], env)
    if before.returncode != 0:
        return {**out, "skipped": "git status failed"}
    if before.stdout.strip():
        # Untracked files are never staged, so only modified/staged tracked files matter. Never
        # fold someone's uncommitted work into a "formatter" commit.
        return {**out, "skipped": "worktree not clean"}
    deadline = time.monotonic() + TIMEOUT_S
    for cmd in cmds:
        try:
            r = gate._run(["sh", "-c", cmd], env, deadline, cwd=wt)
            out["ran"].append({"cmd": cmd, "rc": r.returncode})
        except gate._Timeout:
            out["ran"].append({"cmd": cmd, "rc": None, "timeout": True})
            break
        except OSError as e:
            out["ran"].append({"cmd": cmd, "rc": None, "error": str(e)[:200]})
    # Tracked files only: formatters rewrite existing files, and anything untracked a fixer
    # leaves behind (caches, reports) must not be committed.
    changed = _lines(_git(wt, ["diff", "--name-only"], env))
    if not changed:
        return out
    ident = gitident.identity(repo_path)
    if ident is None:
        _revert(wt, env)
        return {**out, "skipped": "no git identity"}
    cenv = {**env, "GIT_AUTHOR_NAME": ident[0], "GIT_AUTHOR_EMAIL": ident[1],
            "GIT_COMMITTER_NAME": ident[0], "GIT_COMMITTER_EMAIL": ident[1]}
    add = _git(wt, ["add", "-u"], cenv)
    commit = _git(wt, ["commit", "--no-verify", "-q", "-m", COMMIT_MSG], cenv) \
        if add.returncode == 0 else add
    if commit.returncode != 0:
        _revert(wt, cenv)
        return {**out, "skipped": "commit failed"}
    out["committed"] = True
    out["files"] = changed
    return out


def _revert(wt: Path, env: dict) -> None:
    _git(wt, ["reset", "-q"], env)
    _git(wt, ["checkout", "--", "."], env)

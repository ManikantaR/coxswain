"""Unit test for worktree._fetch — never branch from a stale base.

Standalone script like the other coxd tests (no pytest harness). Run:

    cd coxd && COXD_HOME=$(mktemp -d) ../.venv/bin/python test_worktree.py

Verifies:
  - a repo with NO remote (local scratch) skips the fetch and still creates a worktree
  - a repo whose remote fetch FAILS raises (it used to be swallowed — the NAS
    container lost its git credentials on rebuild and workers branched from a
    weeks-old clone)
  - a repo with a reachable remote fetches and creates the worktree
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-test-")

import worktree  # noqa: E402


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ,
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


def _repo(root: Path, name: str) -> Path:
    p = root / name
    p.mkdir()
    _git("init", "-q", "-b", "main", cwd=p)
    (p / "f.txt").write_text("x")
    _git("add", ".", cwd=p)
    _git("commit", "-q", "-m", "init", cwd=p)
    return p


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="coxd-wt-"))
    fails = 0

    scratch = _repo(root, "scratch")
    wt = worktree.create(scratch, "scratch-1")
    fails += not (wt / "f.txt").exists()
    print("no remote → worktree created:", (wt / "f.txt").exists())

    broken = _repo(root, "broken")
    _git("remote", "add", "origin", str(root / "does-not-exist.git"), cwd=broken)
    try:
        worktree.create(broken, "broken-1")
        print("FAIL: broken remote did not raise")
        fails += 1
    except RuntimeError as e:
        print("broken remote → raised:", "stale base" in str(e))
        fails += "stale base" not in str(e)

    upstream = _repo(root, "upstream")
    clone = root / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream), str(clone)],
                   check=True, capture_output=True)
    wt2 = worktree.create(clone, "clone-1")
    fails += not (wt2 / "f.txt").exists()
    print("reachable remote → worktree created:", (wt2 / "f.txt").exists())

    print("OK" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

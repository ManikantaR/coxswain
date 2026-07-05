"""Shared fixtures (T-04). All unit tests fake subprocesses via fake_proc."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from cox import proc


@pytest.fixture(autouse=True)
def cox_home(tmp_path, monkeypatch):
    """Isolate every test's coxswain home to a tmp dir."""
    h = tmp_path / "home"
    monkeypatch.setenv("COX_HOME", str(h))
    (h / "state").mkdir(parents=True)
    (h / "data").mkdir(parents=True)
    return h


class _ProcRecorder:
    """Match expected command prefixes to canned ProcResults; fail on surprises."""

    def __init__(self) -> None:
        self._rules: list[tuple[list[str], proc.ProcResult]] = []
        self.calls: list[list[str]] = []

    def expect(self, prefix: Sequence[str], rc: int = 0, out: str = "", err: str = "") -> None:
        self._rules.append((list(prefix), proc.ProcResult(rc, out, err)))

    def __call__(self, cmd, *, cwd=None, env=None, timeout=None, ok_rc=(0,)):
        self.calls.append(list(cmd))
        for prefix, result in self._rules:
            if list(cmd)[: len(prefix)] == prefix:
                if result.rc not in ok_rc:
                    raise proc.BosunProcError(cmd, result.rc, result.out, result.err)
                return result
        raise AssertionError(f"unexpected command: {list(cmd)}")


@pytest.fixture
def fake_proc(monkeypatch) -> _ProcRecorder:
    rec = _ProcRecorder()
    monkeypatch.setattr(proc, "run", rec)
    return rec


@pytest.fixture
def git_repo(tmp_path) -> Callable[[], Path]:
    """Factory for a real local git repo with an origin bare remote."""

    def _make(name: str = "repo") -> Path:
        bare = tmp_path / f"{name}.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True)
        work = tmp_path / name
        subprocess.run(["git", "init", "-b", "main", str(work)], check=True)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        import os

        e = {**os.environ, **env}
        (work / "README.md").write_text("hi\n")
        subprocess.run(["git", "add", "-A"], cwd=work, check=True, env=e)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=work, check=True, env=e)
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=work, check=True)
        subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=work, check=True, env=e)
        return work

    return _make

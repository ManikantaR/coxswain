"""Pre-gate autofix: runs the registry's fixers, commits ONLY their output.

Standalone: cd coxd && COXD_HOME=$(mktemp -d) python test_autofix.py   (also runs under pytest)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
tempfile.tempdir = tempfile.mkdtemp(prefix="coxd-af-tmp-")  # envscrub's scratch home lives here
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-af-")
os.environ.pop("COXD_WORKER_USER", None)
os.environ["COXD_GIT_NAME"] = "coxd-test"
os.environ["COXD_GIT_EMAIL"] = "coxd-test@example.invalid"

import autofix  # noqa: E402
import registry  # noqa: E402

# A fake formatter: strips trailing whitespace from tracked *.txt files.
FIXER = "sed -i 's/[ \\t]*$//' messy.txt"


def git(wt: Path, *a: str) -> str:
    r = subprocess.run(["git", *a], cwd=wt, capture_output=True, text=True, check=True)
    return r.stdout


def make_repo(messy: str = "a  \nb \n") -> Path:
    wt = Path(tempfile.mkdtemp(prefix="coxd-af-wt-"))
    git(wt, "init", "-q", "-b", "main")
    git(wt, "config", "user.name", "t")
    git(wt, "config", "user.email", "t@example.invalid")
    (wt / "messy.txt").write_text(messy)
    (wt / "other.txt").write_text("keep  \n")  # also messy, but the fixer never touches it
    git(wt, "add", ".")
    git(wt, "commit", "-q", "-m", "init")
    return wt


def commits(wt: Path) -> list[str]:
    return git(wt, "log", "--format=%s").splitlines()


def test_commits_only_fixer_output():
    wt = make_repo()
    (wt / "scratch.log").write_text("untracked fixer by-product\n")  # must not be committed
    # (it pre-exists here; a fixer-created untracked file takes the same path: never staged)
    res = autofix.run(wt, {"autofix": [FIXER]})
    assert res["committed"] is True and res["files"] == ["messy.txt"], res
    assert commits(wt)[0] == autofix.COMMIT_MSG and len(commits(wt)) == 2
    assert git(wt, "show", "--name-only", "--format=", "HEAD").split() == ["messy.txt"]
    assert (wt / "messy.txt").read_text() == "a\nb\n"
    assert (wt / "other.txt").read_text() == "keep  \n"
    assert "scratch.log" in git(wt, "status", "--porcelain")  # still untracked, not committed
    assert "coxd-test" in git(wt, "log", "-1", "--format=%an")


def test_no_commit_when_nothing_changes():
    wt = make_repo("clean\n")
    res = autofix.run(wt, {"autofix": [FIXER]})
    assert res["committed"] is False and res["files"] == [] and res["ran"][0]["rc"] == 0
    assert len(commits(wt)) == 1


def test_failing_fixer_is_non_fatal():
    wt = make_repo()
    res = autofix.run(wt, {"autofix": ["exit 3", "nonexistent-fixer-xyz"]})
    assert [r["rc"] for r in res["ran"]] != [] and all(r["rc"] != 0 for r in res["ran"])
    assert res["committed"] is False and len(commits(wt)) == 1


def test_dirty_tree_is_not_committed():
    wt = make_repo()
    (wt / "other.txt").write_text("someone's uncommitted work\n")
    res = autofix.run(wt, {"autofix": [FIXER]})
    assert res["ran"] == [] and res["committed"] is False and "not clean" in res["skipped"]
    assert (wt / "messy.txt").read_text() == "a  \nb \n"  # fixer never ran
    assert len(commits(wt)) == 1


def test_opt_in_and_no_identity_reverts():
    wt = make_repo()
    assert autofix.run(wt, {}) == {"ran": [], "committed": False, "files": []}  # no key: off
    assert registry.autofix_cmds({"autofix": "ruff format ."}) == []  # not a list: off
    saved = {k: os.environ.pop(k) for k in ("COXD_GIT_NAME", "COXD_GIT_EMAIL")}
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"):
        saved.setdefault(k, os.environ.pop(k, ""))
    try:
        res = autofix.run(wt, {"autofix": [FIXER]}, repo_path=Path(tempfile.mkdtemp()))
    finally:
        os.environ.update({k: v for k, v in saved.items() if v})
    # Unidentifiable author: fixer output must be reverted, never left uncommitted.
    assert res["committed"] is False and git(wt, "status", "--porcelain") == ""
    assert (wt / "messy.txt").read_text() == "a  \nb \n"


def test_scout_seeds_autofix_for_new_repos_only():
    d = Path(tempfile.mkdtemp())
    (d / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    assert registry.scout(d)["autofix"] == ["ruff check --fix ."]  # no CI enforcing format
    (d / ".github" / "workflows").mkdir(parents=True)
    (d / ".github" / "workflows" / "ci.yml").write_text("jobs:\n  a:\n    steps:\n"
                                                         "      - run: ruff format --check .\n")
    assert registry.scout(d)["autofix"] == ["ruff check --fix .", "ruff format ."]


def test_loop_autofixes_before_every_gate_and_is_not_a_fix_round():
    import asyncio
    from types import SimpleNamespace

    import gate
    import lane
    import loop
    import notify
    import store

    order: list[str] = []
    gates = iter([{"passed": False, "failing": "lint", "reason": "x", "steps": {}},
                  {"passed": True, "steps": {}}])
    real_run = autofix.run
    saved = [(o, a, getattr(o, a)) for o, a in (
        (gate, "run_gate"), (gate, "head_sha"), (gate, "review_diff"),
        (notify, "notify_async"), (lane, "review"), (loop, "_resume_fix"))]
    saved_ship = sys.modules.get("ship")
    autofix.run = lambda wt, entry, repo_path=None: (  # type: ignore[assignment]
        order.append("autofix") or {"ran": [], "committed": False, "files": []})
    gate.run_gate = lambda wt, entry: (order.append("gate") or next(gates))  # type: ignore
    gate.head_sha = lambda wt: "a" * 40  # type: ignore
    gate.review_diff = lambda wt, entry=None, base=None, budget=0: "D"  # type: ignore
    notify.notify_async = lambda *a, **k: None  # type: ignore
    sys.modules["ship"] = SimpleNamespace(ship=lambda tid: ("local", None))

    async def review(*a, **k):
        return SimpleNamespace(outcome="reviewed", verdict="approve", cost=0.0,
                               quota_reset_at=None, findings=[])

    async def resume_fix(*a, **k):
        order.append("fix")
        return False

    lane.review = review  # type: ignore
    loop._resume_fix = resume_fix  # type: ignore
    store.create_task("afl1", "example", "b", "/wt", repo_path="/repo/example")
    try:
        state = asyncio.run(loop._gate_review_ship(
            "afl1", Path("/wt"), "example", {"autofix": ["x"]}, "w", "r", "low",
            lambda k, d: store.append_event("afl1", k, d)))
    finally:
        autofix.run = real_run  # type: ignore[assignment]
        for o, a, v in saved:  # don't leak patches into other tests in the same process
            setattr(o, a, v)
        if saved_ship is None:
            sys.modules.pop("ship", None)
        else:
            sys.modules["ship"] = saved_ship
    assert state == "pr_ready", state
    assert order == ["autofix", "gate", "fix", "autofix", "gate"], order
    kinds = [e["kind"] for e in store.events("afl1")]
    assert kinds.count("autofix") == 2 and kinds.count("fix-trigger") == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)

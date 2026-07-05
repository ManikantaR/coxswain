"""Integration tests against real temp git repos (T-05, T-07, T-09, T-10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cox import fix as fixmod
from cox import gate, review, store, worktree
from cox.lanes.claude import parse_stream_json
from cox.model import DispatchPath, TaskMeta, TaskState

FIXTURES = Path(__file__).parent / "fixtures"


# --- T-05 worktree ---
def test_worktree_create(git_repo):
    repo = git_repo()
    wt = worktree.create(repo, "repo-t-1")
    assert wt.path.exists()
    assert wt.branch == "cox/repo-t-1"
    assert (wt.path / "README.md").exists()


def test_worktree_create_bad_repo(tmp_path):
    from cox import proc

    with pytest.raises(proc.BosunProcError):
        worktree.create(tmp_path / "nope", "x-1")


# --- T-07 claude parse ---
def test_parse_stream_json_fixture():
    res = parse_stream_json(FIXTURES / "claude-stream.jsonl", phase="implement")
    assert res.outcome == "success"
    assert res.session_id == "e5fd32ca-1111-2222-3333-444455556666"
    assert res.cost is not None
    assert res.cost.cost_usd == pytest.approx(0.0421)
    assert res.cost.tokens_in == 8120  # input + cache_read
    assert res.cost.tokens_out == 340


def test_parse_stream_json_garbage(tmp_path):
    log = tmp_path / "bad.log"
    log.write_text("not json\n{also not\n")
    res = parse_stream_json(log, phase="implement")
    assert res.outcome == "parse-error"
    assert res.session_id is None


# --- T-09 gate ---
def _dispatched_meta(repo: Path, tid: str) -> TaskMeta:
    wt = worktree.create(repo, tid)
    m = TaskMeta(
        id=tid, repo="repo", worktree=str(wt.path), branch=wt.branch,
        lane="stub", model="stub:0", path=DispatchPath.FULL, state=TaskState.GATING,
    )
    store.save_meta(m)
    return m


def _write_repo_cfg(worktree_path: Path, *, test="true", lint="true", review="none"):
    (worktree_path / ".cox").mkdir(exist_ok=True)
    (worktree_path / ".cox" / "repo.yml").write_text(
        f"commands:\n  test: '{test}'\n  lint: '{lint}'\nreview: {review}\n"
        f"target_branch: main\nscm: local\n"
    )


def test_gate_green(git_repo):
    repo = git_repo()
    m = _dispatched_meta(repo, "repo-green-1")
    wt = Path(m.worktree)
    _write_repo_cfg(wt)
    (wt / "evidence").mkdir(parents=True, exist_ok=True)  # data dir path is separate
    ev = store.task_data_dir(m.id) / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "test-output.txt").write_text("1 passed\n")
    report = gate.run_gate(m.id)
    assert report.passed, report.steps


def test_gate_red_on_failing_test(git_repo):
    repo = git_repo()
    m = _dispatched_meta(repo, "repo-red-1")
    _write_repo_cfg(Path(m.worktree), test="false")
    report = gate.run_gate(m.id)
    assert not report.passed
    assert report.failing_step == "test"
    assert (store.task_data_dir(m.id) / "feedback.md").exists()


def test_gate_evidence_missing(git_repo):
    repo = git_repo()
    m = _dispatched_meta(repo, "repo-noev-1")
    _write_repo_cfg(Path(m.worktree))
    report = gate.run_gate(m.id)
    assert not report.passed
    assert report.failing_step == "evidence"


# --- T-10 review routing ---
@pytest.mark.parametrize(
    "raw,route",
    [
        ('{"findings":[],"verdict":"approve"}', "approve"),
        ('{"findings":[{"action":"auto-fix"}],"verdict":"fix"}', "auto-fix"),
        ('{"findings":[{"action":"ask-user"}],"verdict":"fix"}', "ask-user"),
        ('prose {"findings":[],"verdict":"reject"} trailing', "ask-user"),
    ],
)
def test_review_parse_routing(raw, route):
    out = review.parse_review(raw)
    assert out is not None and out.route == route


def test_review_parse_unparseable():
    assert review.parse_review("no json here") is None


# --- T-10 fix cap ---
def test_fix_cap(git_repo, monkeypatch):
    repo = git_repo()
    wt = worktree.create(repo, "repo-fix-1")
    m = TaskMeta(
        id="repo-fix-1", repo="repo", worktree=str(wt.path), branch=wt.branch,
        lane="stub", model="stub:0", path=DispatchPath.FULL, state=TaskState.FIXING,
        session_id="s", fix_rounds=fixmod.MAX_FIX_ROUNDS,
    )
    store.save_meta(m)
    with pytest.raises(fixmod.FixCapReached):
        fixmod.fix("repo-fix-1")
    assert store.load_meta("repo-fix-1").state is TaskState.NEEDS_HUMAN

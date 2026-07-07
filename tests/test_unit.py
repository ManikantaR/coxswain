"""Unit tests for the tokenless core (T-01..T-10)."""

from __future__ import annotations

import json

import pytest

from cox import classify, home, models, proc, store, wakequeue
from cox.cli import main
from cox.model import CostEntry, DispatchPath, NeedsHumanReason, TaskMeta, TaskState


# --- T-01 home + pause ---
def test_home_and_pause():
    assert home.home().name == "home"
    assert not home.is_paused()
    home.set_paused(True)
    assert home.is_paused()
    home.set_paused(False)
    assert not home.is_paused()


# --- T-02 model + store ---
def test_meta_round_trip():
    m = TaskMeta(
        id="repo-x-2601010000", repo="repo", worktree="/w", branch="cox/x",
        lane="claude", model="sonnet:medium", path=DispatchPath.FULL,
        state=TaskState.WORKING, reason=None,
    )
    store.save_meta(m)
    got = store.load_meta(m.id)
    assert got == m
    assert got.state is TaskState.WORKING


def test_meta_reason_serialization():
    m = TaskMeta(
        id="r-y-1", repo="r", worktree="/w", branch="b", lane="claude",
        model="s:m", path=DispatchPath.QUICK, state=TaskState.NEEDS_HUMAN,
        reason=NeedsHumanReason.GATE_RED,
    )
    store.save_meta(m)
    assert store.load_meta(m.id).reason is NeedsHumanReason.GATE_RED


def test_status_append_and_cost_total():
    tid = "r-z-1"
    store.append_status(tid, "working: a")
    store.append_status(tid, "done: b")
    log = (store.task_data_dir(tid) / "status.log").read_text()
    assert log.splitlines() == ["working: a", "done: b"]
    store.append_cost(tid, CostEntry("implement", 100, 10, 0.01))
    store.append_cost(tid, CostEntry("fix", 5, 2, 0.001))
    tin, tout, cost = store.cost_total(tid)
    assert (tin, tout) == (105, 12)
    assert cost == pytest.approx(0.011)


def test_cost_total_unknown_when_any_none():
    tid = "r-z-2"
    store.append_cost(tid, CostEntry("implement", 1, 1, None))
    _, _, cost = store.cost_total(tid)
    assert cost is None


# --- T-03 models ---
def test_models_default_pinned():
    # default lane is claude -> a pinned Sonnet; reviewer stays Opus
    spec = models.resolve("implementer")
    assert spec.model == "claude-sonnet-4-6" and spec.effort == "medium"
    assert models.resolve("reviewer").model == "opus"


def test_models_env_override(monkeypatch):
    monkeypatch.setenv("COX_MODEL_REVIEW", "haiku:low")
    spec = models.resolve("reviewer")
    assert spec.model == "haiku" and spec.effort == "low"


def test_models_repo_override(tmp_path):
    (tmp_path / ".cox").mkdir()
    (tmp_path / ".cox" / "repo.yml").write_text(
        "models:\n  implementer:\n    model: opus\n    effort: high\n"
    )
    spec = models.resolve("implementer", repo_path=tmp_path)
    assert spec.model == "opus" and spec.effort == "high"


def test_models_bad_yaml_crashes(tmp_path):
    gpath = tmp_path / "models.yml"
    gpath.write_text("this: [is: unbalanced\n")
    import os

    os.environ["COX_MODELS_FILE"] = str(gpath)
    try:
        with pytest.raises(models.BosunConfigError):
            models.resolve("implementer")
    finally:
        del os.environ["COX_MODELS_FILE"]


def test_models_command_prints_resolved_routing(capsys):
    rc = main(["models"])
    assert rc == 0

    impl_claude = models.resolve("implementer", lane="claude")
    impl_codex = models.resolve("implementer", lane="codex")
    impl_stub = models.resolve("implementer", lane="stub")
    reviewer = models.resolve("reviewer")
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        f"implementer  claude -> {impl_claude.model}:{impl_claude.effort}",
        f"implementer  codex  -> {impl_codex.model}:{impl_codex.effort}",
        f"implementer  stub   -> {impl_stub.model}:{impl_stub.effort}",
        f"reviewer     (all)  -> {reviewer.model}:{reviewer.effort}",
    ]


# --- T-04 proc ---
def test_run_raises_on_nonzero():
    with pytest.raises(proc.BosunProcError):
        proc.run(["sh", "-c", "exit 3"])
    assert proc.run(["sh", "-c", "exit 3"], ok_rc=(3,)).rc == 3


def test_spawn_detached(tmp_path):
    log = tmp_path / "l.log"
    pid = proc.spawn_detached(
        ["sh", "-c", "echo hi"], log_path=log, pid_path=tmp_path / "pid"
    )
    assert (tmp_path / "pid").read_text() == str(pid)
    import time

    for _ in range(50):
        if log.exists() and b"hi" in log.read_bytes():
            break
        time.sleep(0.05)
    assert b"hi" in log.read_bytes()


# --- T-08 classify ---
@pytest.mark.parametrize(
    "line,verb",
    [
        ("done: shipped", "done"),
        ("failed: boom", "failed"),
        ("blocked: waiting", "blocked"),
        ("pr-ready: http://x", "pr-ready"),
        ("ci-red: checks", "ci-red"),
    ],
)
def test_classify_actionable(line, verb):
    w = classify.classify(line)
    assert w is not None and w.verb == verb


@pytest.mark.parametrize("line", ["working: step 3", "PROGRESS 2026", "", "   ", "note: hi"])
def test_classify_benign(line):
    assert classify.classify(line) is None


# --- wakequeue dedupe + crash replay ---
def test_wakequeue_dedupe_and_delivery():
    assert wakequeue.enqueue("t1", "done", "x", "done: x") is True
    assert wakequeue.enqueue("t1", "done", "x", "done: x") is False  # dupe
    pending = wakequeue.undelivered()
    assert len(pending) == 1
    wakequeue.mark_delivered({pending[0].key})
    assert wakequeue.undelivered() == []
    # Re-enqueue same source line is still deduped after delivery.
    assert wakequeue.enqueue("t1", "done", "x", "done: x") is False


# --- status --json ---
def test_status_json_empty_home(capsys):
    rc = main(["status", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out) == []


def test_status_json_one_task(capsys):
    m = TaskMeta(
        id="myrepo-feat-2601010000",
        repo="myrepo",
        worktree="/wt/myrepo-feat",
        branch="cox/feat",
        lane="claude",
        model="sonnet:medium",
        path=DispatchPath.FULL,
        state=TaskState.WORKING,
        reason=None,
        dispatched_at=1_700_000_000.0,
    )
    store.save_meta(m)
    store.append_cost(m.id, CostEntry("implement", 200, 50, 0.05))

    rc = main(["status", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    rows = json.loads(out)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "myrepo-feat-2601010000"
    assert row["repo"] == "myrepo"
    assert row["state"] == "working"
    assert row["reason"] is None
    assert row["path"] == "full"
    assert row["lane"] == "claude"
    assert row["model"] == "sonnet:medium"
    assert abs(row["cost_usd"] - 0.05) < 1e-9
    assert row["dispatched_at"] == 1_700_000_000.0


def test_status_json_reason_and_no_cost(capsys):
    m = TaskMeta(
        id="myrepo-bug-2601010001",
        repo="myrepo",
        worktree="/wt/myrepo-bug",
        branch="cox/bug",
        lane="claude",
        model="opus:high",
        path=DispatchPath.QUICK,
        state=TaskState.NEEDS_HUMAN,
        reason=NeedsHumanReason.GATE_RED,
        dispatched_at=1_700_001_000.0,
    )
    store.save_meta(m)
    # No cost entries — cost_usd should be null.

    rc = main(["status", "--json"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    row = rows[0]
    assert row["reason"] == "gate-red"
    assert row["cost_usd"] is None

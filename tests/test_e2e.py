"""End-to-end loop with the stub lane — zero tokens, zero network (T-13).

dispatch -> worker (stub) -> watcher scan -> gate -> review(none) -> ship(local)
-> merge -> landed. This is the proof that the loop is wired correctly and can
run in CI without spending anything.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from cox import dispatch, gate, plan, ship, store, watch
from cox.model import DispatchPath, NeedsHumanReason, TaskState


def _add_repo_config(repo: Path) -> None:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    import os

    e = {**os.environ, **env}
    (repo / ".cox").mkdir(exist_ok=True)
    (repo / ".cox" / "repo.yml").write_text(
        "commands:\n  test: 'true'\n  lint: 'true'\nreview: none\n"
        "target_branch: main\nscm: local\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=e)
    subprocess.run(["git", "commit", "-q", "-m", "add cox config"], cwd=repo, check=True, env=e)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, check=True, env=e)


def _wait_for_done(task_id: str, timeout: float = 15) -> None:
    log = store.task_data_dir(task_id) / "status.log"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log.exists() and "done:" in log.read_text():
            return
        time.sleep(0.1)
    raise AssertionError(f"worker did not finish: {log.read_text() if log.exists() else 'no log'}")


def test_full_loop_stub_lane(git_repo):
    repo = git_repo()
    _add_repo_config(repo)

    meta = dispatch.dispatch(
        repo_path=repo,
        title="add a stub change",
        body="Make a trivial change and prove the loop.",
        path=DispatchPath.FULL,
        lane="stub",
    )
    assert meta.state is TaskState.WORKING

    _wait_for_done(meta.id)

    # Watcher turns the worker's `done:` into an actionable wake.
    watch.scan_once()
    from cox import wakequeue

    verbs = {w.verb for w in wakequeue.undelivered()}
    assert "done" in verbs

    # Gate (review=none so no model call).
    report = gate.run_gate(meta.id)
    assert report.passed, report.steps

    # Ship + merge via the local SCM.
    m2 = ship.ship(meta.id, repo, "add a stub change")
    assert m2.pr_url and m2.pr_url.startswith("local://")
    m3 = ship.merge(meta.id, repo)
    assert m3.state is TaskState.LANDED
    # merge logs a cross-task history record for the trend (D1)
    hist = store.read_history()
    assert hist and hist[-1]["id"] == meta.id and "cycle_secs" in hist[-1]


def test_plan_phase_with_approval_then_full_loop(git_repo):
    repo = git_repo()
    _add_repo_config(repo)

    # dispatch with a plan slot + approval gate -> PLANNING, not WORKING
    meta = dispatch.dispatch(
        repo_path=repo, title="planned change", body="Do the thing.",
        path=DispatchPath.FULL, lane="stub", plan_lane="stub", plan_approval=True,
    )
    assert meta.state is TaskState.PLANNING
    assert (Path(meta.worktree) / "plan.md").exists()  # architect drafted it

    # finalize -> parks for approval (no implementer yet)
    parked = plan.finalize(meta.id)
    assert parked.state is TaskState.NEEDS_HUMAN
    assert parked.reason is NeedsHumanReason.PLAN_REVIEW

    # captain approves -> implementer starts, brief carries the plan pointer
    approved = plan.approve(meta.id)
    assert approved.state is TaskState.WORKING
    brief = (store.task_data_dir(meta.id) / "brief.md").read_text()
    assert "plan.md" in brief

    # the architect's plan.md carried a 'How to verify' section -> acceptance criteria
    from cox import acceptance

    assert acceptance.load_criteria(meta.id)  # lifted from the stub plan
    brief = (store.task_data_dir(meta.id) / "brief.md").read_text()
    assert "Acceptance criteria" in brief  # injected for the implementer

    _wait_for_done(meta.id)
    # the stub implementer self-verified against the criteria before the gate
    st = acceptance.status(meta.id)
    assert st and all(r["self"] == "pass" for r in st)

    report = gate.run_gate(meta.id)
    assert report.passed, report.steps
    ship.ship(meta.id, repo, "planned change")
    assert ship.merge(meta.id, repo).state is TaskState.LANDED


def test_watcher_auto_advances_plan(git_repo):
    # the stall the MyMoney run hit: a PLANNING task must not sit forever —
    # the watcher finalizes it once the architect is done (approval OFF -> WORKING).
    from cox import watch

    repo = git_repo()
    _add_repo_config(repo)
    m = dispatch.dispatch(
        repo_path=repo, title="t", body="b", path=DispatchPath.FULL,
        lane="stub", plan_lane="stub", plan_approval=False,
    )
    assert m.state is TaskState.PLANNING
    watch.scan_once()  # <- no manual `cox plan-finalize` needed anymore
    assert store.load_meta(m.id).state is TaskState.WORKING


def test_watcher_auto_advances_plan_to_approval_gate(git_repo):
    from cox import watch

    repo = git_repo()
    _add_repo_config(repo)
    m = dispatch.dispatch(
        repo_path=repo, title="t", body="b", path=DispatchPath.FULL,
        lane="stub", plan_lane="stub", plan_approval=True,
    )
    watch.scan_once()
    meta = store.load_meta(m.id)
    assert meta.state is TaskState.NEEDS_HUMAN
    assert meta.reason is NeedsHumanReason.PLAN_REVIEW


def test_plan_phase_without_approval_goes_straight_to_working(git_repo):
    repo = git_repo()
    _add_repo_config(repo)
    meta = dispatch.dispatch(
        repo_path=repo, title="planned no-gate", body="Do it.",
        path=DispatchPath.FULL, lane="stub", plan_lane="stub", plan_approval=False,
    )
    assert meta.state is TaskState.PLANNING
    # finalize with approval off -> implementer starts immediately
    assert plan.finalize(meta.id).state is TaskState.WORKING


def test_pause_blocks_dispatch(git_repo):
    from cox import home

    repo = git_repo()
    home.set_paused(True)
    try:
        raised = False
        try:
            dispatch.dispatch(
                repo_path=repo, title="x", body="x", path=DispatchPath.FULL, lane="stub"
            )
        except dispatch.DispatchError:
            raised = True
        assert raised
    finally:
        home.set_paused(False)


def test_worker_cap(git_repo, monkeypatch):
    repo = git_repo()
    _add_repo_config(repo)
    monkeypatch.setattr(dispatch, "MAX_WORKERS", 1)
    monkeypatch.setattr(dispatch, "HARD_MAX_WORKERS", 1)
    dispatch.dispatch(repo_path=repo, title="one", body="one", path=DispatchPath.FULL, lane="stub")
    raised = False
    try:
        dispatch.dispatch(
            repo_path=repo, title="two", body="two", path=DispatchPath.FULL, lane="stub"
        )
    except dispatch.DispatchError:
        raised = True
    assert raised

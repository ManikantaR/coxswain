"""Cost A2: per-repo `plan_first` — planner (lane.plan) runs once before the worker,
its plan rides in the worker brief, and its cost lands in the ledger as phase `plan`.
Off (the default) leaves the flow untouched. Invented data only."""
import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "coxd"))


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("COXD_HOME", str(tmp_path / "coxd-home"))
    stub = types.ModuleType("claude_agent_sdk")
    for n in ("AssistantMessage", "ClaudeAgentOptions", "ClaudeSDKClient", "HookMatcher",
              "RateLimitEvent", "ResultMessage", "TextBlock", "ToolUseBlock", "query"):
        setattr(stub, n, type(n, (), {}))
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", stub)
    for m in ("lane", "loop", "store", "registry", "stepspend"):
        monkeypatch.delitem(sys.modules, m, raising=False)
    import lane
    import loop
    import registry
    import stepspend
    import store

    calls = {"plan": [], "worker": []}

    async def fake_plan(brief, model, emit, effort="medium", *, worktree):
        calls["plan"].append((brief, model))
        emit("result", {"model": model, "cost": 0.25})
        return lane.PlanOutcome("planned", "1. edit a.py\n2. add a test", 0.25)

    async def fake_worker(wt, prompt, model, emit, resume=None, effort="medium", **k):
        calls["worker"].append(prompt)
        emit("result", {"model": model, "cost": 1.0})
        return SimpleNamespace(cost=1.0, summary="s", session_id=None, quota_reset_at=None,
                               is_error=True)  # stop right after the worker

    monkeypatch.setattr(lane, "plan", fake_plan)
    monkeypatch.setattr(lane, "run_worker", fake_worker)
    monkeypatch.setattr(loop.worktree, "provision", lambda wt: {})
    return SimpleNamespace(loop=loop, store=store, registry=registry, calls=calls,
                           stepspend=stepspend, monkeypatch=monkeypatch)


def _run(e, reg):
    e.monkeypatch.setattr(e.registry, "load", lambda r: reg)
    e.store.create_task("t1", "widgets", "BRIEF", "/tmp/wt")
    asyncio.run(e.loop.run_task("t1"))
    return e.store.events("t1")


def test_off_by_default(env):
    evs = _run(env, {})
    assert env.calls["plan"] == []
    assert env.calls["worker"] == ["BRIEF"]
    assert not any(e["kind"] == "plan" for e in evs)
    assert "planning" not in [e["data"].get("state") for e in evs if e["kind"] == "state"]
    assert env.store.get_task("t1")["cost"] == 1.0


def test_on_plans_once_and_briefs_worker(env):
    evs = _run(env, {"plan_first": True})
    assert len(env.calls["plan"]) == 1 and env.calls["plan"][0][0] == "BRIEF"
    assert len(env.calls["worker"]) == 1
    w = env.calls["worker"][0]
    assert w.startswith("BRIEF") and "1. edit a.py" in w
    assert "Follow this plan; deviate only if it is wrong, and say why." in w
    plan_ev = [e for e in evs if e["kind"] == "plan"]
    assert len(plan_ev) == 1 and plan_ev[0]["data"]["cost"] == 0.25
    with env.store._conn() as c:
        assert c.execute("SELECT SUM(delta) FROM cost_ledger").fetchone()[0] == 1.25
    agg = env.stepspend.aggregate([evs], since=0)
    assert agg["by_phase"]["plan"] == 0.25 and agg["by_phase"]["implement"] == 1.0


def test_plan_failure_falls_back_to_plain_brief(env):
    async def bad_plan(brief, model, emit, effort="medium", *, worktree):
        return env.loop.lane.PlanOutcome("plan-error", None, 0.1)

    env.monkeypatch.setattr(env.loop.lane, "plan", bad_plan)
    evs = _run(env, {"plan_first": True})
    assert env.calls["worker"] == ["BRIEF"]
    assert not any(e["kind"] == "plan" for e in evs)
    assert env.store.get_task("t1")["cost"] == pytest.approx(1.1)

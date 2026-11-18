"""Tests for queue 2/8: planner structured `files`, section-1 fallback, and event metadata."""
from __future__ import annotations

import asyncio
import subprocess
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
    for n in ("AssistantMessage", "ClaudeSDKClient", "RateLimitEvent",
              "TextBlock", "ToolUseBlock", "query"):
        setattr(stub, n, type(n, (), {}))
    for n in ("ClaudeAgentOptions", "HookMatcher"):
        setattr(stub, n, type(n, (), {"__init__": lambda self, **kw: None}))
    stub.ResultMessage = type("ResultMessage", (), {"__init__": lambda self, **kw: None})
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", stub)
    for m in ("agy_handoff", "intake", "lane", "loop", "notify", "registry", "store"):
        monkeypatch.delitem(sys.modules, m, raising=False)
    import agy_handoff
    import lane
    import loop
    import notify
    import registry
    import store

    monkeypatch.setattr(notify, "notify_async", lambda *a, **k: None)
    monkeypatch.setattr(lane.envscrub, "sdk_blanking", lambda: {})
    return SimpleNamespace(
        agy_handoff=agy_handoff,
        lane=lane,
        loop=loop,
        registry=registry,
        store=store,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )


class _FakeResult:
    def __init__(self, structured_output, cost=0.12):
        self.structured_output = structured_output
        self.total_cost_usd = cost
        self.num_turns = 2
        self.stop_reason = "end_turn"
        self.usage = None
        self.model_usage = None
        self.is_error = False


def test_plan_schema_includes_files(env):
    schema = env.lane._PLAN_SCHEMA
    assert "files" in schema["properties"]
    assert schema["properties"]["files"] == {"type": "array", "items": {"type": "string"}}
    assert "plan_md" in schema["required"]
    assert "files" in env.lane._PLAN_PROMPT


def test_plan_outcome_positional_constructor(env):
    pr = env.lane.PlanOutcome("planned", "md", 0.1)
    assert pr.outcome == "planned"
    assert pr.plan_md == "md"
    assert pr.cost == 0.1
    assert pr.quota_reset_at is None
    assert pr.files == []


def test_plan_structured_files_and_sanitization(env):
    async def fake_query(*, prompt, options):
        yield _FakeResult({
            "plan_md": "1. Files to touch\n- `coxd/lane.py`",
            "files": [
                "coxd/lane.py",
                "/etc/x",
                "../x",
                "~/secret.txt",
                "https://example.com/a.py",
                123,
                None,
                "   ",
                "./tests/test_x.py",
                "coxd/lane.py",
            ],
        })

    env.monkeypatch.setattr(env.lane, "ResultMessage", _FakeResult)
    env.monkeypatch.setattr(env.lane, "query", fake_query)
    out = asyncio.run(env.lane.plan("brief", "m", lambda *a: None, worktree=env.tmp_path))
    assert out.outcome == "planned"
    assert out.files == ["coxd/lane.py", "tests/test_x.py"]


def test_plan_fallback_extracts_section_1_only(env):
    sample_md = (
        "## 1. Files to touch\n"
        "- `coxd/lane.py`: update schema\n"
        "- tests/test_x.py: add unit tests\n\n"
        "## 2. Ordered steps\n"
        "1. Do not extract `coxd/only_in_section2.py`.\n"
    )

    for payload in ({"plan_md": sample_md}, {"plan_md": sample_md, "files": []}):
        async def fake_query(*, prompt, options, _payload=payload):
            yield _FakeResult(_payload)

        env.monkeypatch.setattr(env.lane, "ResultMessage", _FakeResult)
        env.monkeypatch.setattr(env.lane, "query", fake_query)
        out = asyncio.run(env.lane.plan("brief", "m", lambda *a: None, worktree=env.tmp_path))
        assert out.outcome == "planned"
        assert out.files == ["coxd/lane.py", "tests/test_x.py"]
        assert "coxd/only_in_section2.py" not in out.files


def test_extract_plan_files_unit_cases(env):
    ex = env.lane.extract_plan_files
    assert ex("") == []
    assert ex("## Overview\n- `a.py`\n") == []

    md = (
        "1. **Files to touch**\n"
        "- `coxd/lane.py:42`\n"
        "- (coxd/loop.py:10-20)\n"
        "- `coxd/lane.py`: duplicate\n"
        "- See https://github.com/o/r/blob/main/ignored.py and version 1.2 or v2.0\n"
        "- Reject /etc/passwd, ~/home.txt, ../escape.py, a/../b.py\n"
        "- `Makefile` and `deploy/Dockerfile`\n"
        "2. Ordered steps\n"
        "- Touch `coxd/later_section.py`\n"
    )
    assert ex(md) == [
        "coxd/lane.py",
        "coxd/loop.py",
        "Makefile",
        "deploy/Dockerfile",
    ]


def test_plan_and_hand_off_stores_files_issue_and_deps(env):
    md = "1. Files to touch\n- `coxd/lane.py`\n\n2. Ordered steps\n- edit\n"

    async def fake_plan(brief, model, emit, effort="medium", *, worktree):
        return env.lane.PlanOutcome("planned", md, 0.1, files=["coxd/lane.py", "tests/test_x.py"])

    def fake_git(cwd, *args):
        if args == ("rev-parse", "HEAD"):
            return subprocess.CompletedProcess(["git", *args], 0, stdout="a" * 40 + "\n", stderr="")
        if args[:2] == ("push", "origin"):
            return subprocess.CompletedProcess(["git", *args], 0, stdout="", stderr="")
        raise AssertionError(f"unexpected git call: {args}")

    env.monkeypatch.setattr(env.lane, "plan", fake_plan)
    env.monkeypatch.setattr(env.loop, "_git", fake_git)
    env.monkeypatch.setattr(env.registry, "load", lambda r: {})

    brief = (
        "Add feature\n\n"
        "Depends on #120\n"
        "Depends on https://github.com/other/repo/issues/99\n\n"
        "GitHub issue: https://github.com/o/r/issues/124"
    )
    env.store.create_task(
        "t-agy", "widgets", brief, str(env.tmp_path),
        repo_path=str(env.tmp_path), lane="agy-handoff",
    )
    res = asyncio.run(env.loop.run_task("t-agy"))
    assert res == "waiting_agy"

    plan_evs = [e for e in env.store.events("t-agy") if e["kind"] == "plan"]
    assert len(plan_evs) == 1
    data = plan_evs[0]["data"]
    assert data["files"] == ["coxd/lane.py", "tests/test_x.py"]
    assert data["issue"] == 124
    assert data["depends_on"] == [120, "other/repo#99"]


def test_plan_first_event_carries_files_issue_and_deps(env):
    async def fake_plan(brief, model, emit, effort="medium", *, worktree):
        return env.lane.PlanOutcome("planned", "1. Files to touch\n- `a.py`", 0.2, files=["a.py"])

    async def fake_worker(wt, prompt, model, emit, resume=None, effort="medium", **k):
        return SimpleNamespace(cost=0.5, summary="s", session_id=None, quota_reset_at=None,
                               is_error=True)

    env.monkeypatch.setattr(env.lane, "plan", fake_plan)
    env.monkeypatch.setattr(env.lane, "run_worker", fake_worker)
    env.monkeypatch.setattr(env.loop.worktree, "provision", lambda wt: {})
    env.monkeypatch.setattr(env.registry, "load", lambda r: {"plan_first": True})

    brief = "Task\n\nDepends on #50\n\nGitHub issue: https://github.com/o/r/issues/55"
    env.store.create_task("t-pf", "widgets", brief, str(env.tmp_path))
    asyncio.run(env.loop.run_task("t-pf"))

    plan_evs = [e for e in env.store.events("t-pf") if e["kind"] == "plan"]
    assert len(plan_evs) == 1
    assert plan_evs[0]["data"]["files"] == ["a.py"]
    assert plan_evs[0]["data"]["issue"] == 55
    assert plan_evs[0]["data"]["depends_on"] == [50]


def test_agy_handoff_plan_files_helper_and_next_round(env):
    brief = "Fix bug\n\nDepends on #12\n\nGitHub issue: https://github.com/o/r/issues/15"
    env.store.create_task("t-h", "widgets", brief, str(env.tmp_path), lane="agy-handoff")
    task = env.store.get_task("t-h")

    assert env.agy_handoff.plan_files(task) == set()
    assert env.agy_handoff.plan_files("t-h") == set()

    env.store.append_event("t-h", "plan", {"plan_md": "m1", "round": 1, "files": ["a.py"]})
    env.store.append_event("t-h", "plan", {"plan_md": "m2", "round": 2, "files": ["b.py", "c.py"]})
    assert env.agy_handoff.plan_files(task) == {"b.py", "c.py"}
    assert env.agy_handoff.plan_files("t-h") == {"b.py", "c.py"}

    # next_round carries files from the previous round and populates issue/depends_on
    env.agy_handoff.next_round("t-h", "## Fix these review findings\n", "agy", "review")
    evs = [e for e in env.store.events("t-h") if e["kind"] == "plan"]
    assert evs[-1]["data"]["round"] == 3
    assert evs[-1]["data"]["files"] == ["b.py", "c.py"]
    assert evs[-1]["data"]["issue"] == 15
    assert evs[-1]["data"]["depends_on"] == [12]

    # Fallback to regex when latest event lacks `files` key
    env.store.create_task("t-old", "widgets", "legacy", str(env.tmp_path), lane="agy-handoff")
    legacy_md = "1. Files to touch\n- `coxd/old.py`\n2. Ordered steps\n- `coxd/skip.py`\n"
    env.store.append_event("t-old", "plan", {"plan_md": legacy_md, "round": 1})
    assert env.agy_handoff.plan_files(env.store.get_task("t-old")) == {"coxd/old.py"}

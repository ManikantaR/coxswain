"""Codex lane (T-15): JSONL parse + spawn argv + lane-aware model resolution."""

from __future__ import annotations

from pathlib import Path

from cox import models
from cox.lanes.codex import CodexLane, parse_codex_jsonl
from cox.model import ModelSpec

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_codex_jsonl_fixture():
    res = parse_codex_jsonl(FIXTURES / "codex-stream.jsonl", phase="implement")
    assert res.outcome == "success"
    assert res.session_id == "019f3c2a-707b-7810-998c-f713653e27d9"
    assert res.cost is not None
    assert res.cost.cost_usd is None  # codex reports tokens, no dollar cost
    assert res.cost.tokens_in == 18719  # input + cached_input
    assert res.cost.tokens_out == 190  # output + reasoning
    assert "implemented the feature" in res.raw_tail


def test_parse_codex_jsonl_missing_log(tmp_path):
    res = parse_codex_jsonl(tmp_path / "nope.log", phase="implement")
    assert res.outcome == "parse-error"
    assert res.session_id is None


def test_codex_spawn_argv(tmp_path, monkeypatch):
    from cox.lanes import codex as codexmod

    recorded: dict[str, list[str]] = {}

    def fake_spawn(argv, *, log_path, pid_path, cwd, env):
        recorded["argv"] = list(argv)
        return 777

    monkeypatch.setattr(codexmod.proc, "spawn_detached", fake_spawn)

    data_dir = tmp_path / "data" / "task-1"
    data_dir.mkdir(parents=True)
    brief = data_dir / "brief.md"
    brief.write_text("do the thing", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()

    CodexLane().spawn(
        brief_path=brief,
        worktree=wt,
        model=ModelSpec(provider="openai", model="gpt-5.4", effort="medium"),
        log_path=data_dir / "worker.log",
        pid_path=data_dir / "pid",
    )
    argv = recorded["argv"]
    assert argv[:3] == ["codex", "exec", "--json"]
    assert argv[argv.index("-m") + 1] == "gpt-5.4"
    assert argv[argv.index("--add-dir") + 1] == str(data_dir)
    assert argv[argv.index("-C") + 1] == str(wt)
    assert argv[-1] == "do the thing"  # brief is the lone trailing positional


def test_lane_aware_implementer_defaults():
    # no config/env -> each lane gets its family's model; reviewer stays opus
    assert models.resolve("implementer", lane="claude").model == "claude-sonnet-4-6"
    assert models.resolve("implementer", lane="codex").model == "gpt-5.4"
    assert models.resolve("reviewer", lane="codex").model == "opus"

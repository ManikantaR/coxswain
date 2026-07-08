"""Integration tests against real temp git repos (T-05, T-07, T-09, T-10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cox import fix as fixmod
from cox import gate, review, store, worktree
from cox.lanes.claude import ClaudeLane, parse_stream_json
from cox.model import DispatchPath, ModelSpec, TaskMeta, TaskState

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


# --- _ALLOWED_TOOLS includes mypy and python entries (OBS-tool-friction) ---
def test_allowed_tools_includes_mypy_and_python():
    from cox.lanes.claude import _ALLOWED_TOOLS

    entries = [e.strip() for e in _ALLOWED_TOOLS.split(",")]
    assert "Bash(mypy*)" in entries
    assert "Bash(python*)" in entries
    assert "Bash(python3*)" in entries


# --- T-07 claude spawn argv (shakedown 2026-07-05: worker must be granted the
# task data dir, which is outside the worktree, or status/evidence writes are
# sandbox-blocked) ---
def test_spawn_grants_data_dir(tmp_path, monkeypatch):
    from cox.lanes import claude as claudemod

    recorded: dict[str, list[str]] = {}

    def fake_spawn(argv, *, log_path, pid_path, cwd, env):
        recorded["argv"] = list(argv)
        return 4242

    monkeypatch.setattr(claudemod.proc, "spawn_detached", fake_spawn)

    data_dir = tmp_path / "data" / "task-1"
    data_dir.mkdir(parents=True)
    brief = data_dir / "brief.md"
    brief.write_text("do the thing", encoding="utf-8")
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()

    handle = ClaudeLane().spawn(
        brief_path=brief,
        worktree=worktree_path,
        model=ModelSpec(provider="anthropic", model="sonnet", effort="medium"),
        log_path=data_dir / "worker.log",
        pid_path=data_dir / "pid",
    )

    assert handle.pid == 4242
    argv = recorded["argv"]
    assert "--add-dir" in argv
    i = argv.index("--add-dir")
    assert argv[i + 1] == str(data_dir)
    # BUG-02: --add-dir is variadic; the token after its dir MUST be a flag, never
    # the brief, or claude swallows the brief as a second directory and errors.
    assert argv[i + 2].startswith("--")
    # the brief is the lone trailing positional
    assert argv[-1] == "do the thing"


# --- T-09 ingest worker result: cost + session_id (shakedown BUG-03) ---
def test_ingest_worker_result_records_cost_and_session():
    from cox import gate

    tid = "repo-ingest-1"
    meta = TaskMeta(
        id=tid, repo="repo", worktree="/tmp/wt", branch="cox/x",
        lane="claude", model="sonnet:medium", path=DispatchPath.FULL,
        state=TaskState.GATING,
    )
    store.save_meta(meta)
    log = store.task_data_dir(tid) / "worker.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text((FIXTURES / "claude-stream.jsonl").read_text(), encoding="utf-8")

    gate.ingest_worker_result(tid)

    costs = store.read_cost(tid)
    assert [c.phase for c in costs] == ["implement"]
    assert costs[0].cost_usd == pytest.approx(0.0421)
    assert store.load_meta(tid).session_id == "e5fd32ca-1111-2222-3333-444455556666"

    # idempotent: a second call must not double-count the implement cost
    gate.ingest_worker_result(tid)
    assert len(store.read_cost(tid)) == 1


# --- T-10 review is never re-run: cached verdict short-circuits spawn (DESIGN P2,
# shakedown BUG-05 double-spend guard) ---
def test_review_returns_cached_verdict_without_respawn(tmp_path, monkeypatch):
    from cox import review

    tid = "repo-review-1"
    wt = tmp_path / "wt"
    wt.mkdir()
    meta = TaskMeta(
        id=tid, repo="repo", worktree=str(wt), branch="cox/x",
        lane="claude", model="sonnet:medium", path=DispatchPath.FULL,
        state=TaskState.GATING,
    )
    store.save_meta(meta)
    # a prior verdict exists on disk
    import json as _json

    store.task_data_dir(tid).mkdir(parents=True, exist_ok=True)
    (store.task_data_dir(tid) / "review.json").write_text(
        _json.dumps({"verdict": "approve", "findings": []}), encoding="utf-8"
    )
    # repo.yml with review=full so we don't take the review=none early return
    (wt / ".cox").mkdir()
    (wt / ".cox" / "repo.yml").write_text("review: full\n", encoding="utf-8")

    # spawning would be a re-run — forbid it
    def boom(*a, **k):
        raise AssertionError("review must not re-spawn when a verdict is cached")

    monkeypatch.setattr(review.proc, "spawn_detached", boom)

    out = review.review(tid)
    assert out.route == "approve"


def test_review_argv_is_lane_aware(tmp_path):
    from cox import review
    from cox.model import ModelSpec

    wt = tmp_path / "wt"
    claude = review._review_argv("claude", ModelSpec("anthropic", "opus", "medium"), "P", wt)
    assert claude[0] == "claude" and "--permission-mode" in claude and "plan" in claude
    codex = review._review_argv("codex", ModelSpec("openai", "gpt-5.4", "high"), "P", wt)
    assert codex[:3] == ["codex", "exec", "--json"]
    assert "-s" in codex and codex[codex.index("-s") + 1] == "read-only"  # never edits
    assert "model_reasoning_effort=high" in codex
    assert codex[-1] == "P"  # brief is the lone trailing positional


def test_review_runs_on_codex_lane_when_selected(tmp_path, monkeypatch):
    import json as _json

    from cox import review

    tid = "repo-rev-codex"
    wt = tmp_path / "wt"
    (wt / ".cox").mkdir(parents=True)
    (wt / ".cox" / "repo.yml").write_text("review: full\n", encoding="utf-8")
    meta = TaskMeta(
        id=tid, repo="repo", worktree=str(wt), branch="cox/x", lane="claude",
        model="sonnet:medium", path=DispatchPath.FULL, state=TaskState.GATING,
        review_lane="codex", review_model="gpt-5.4:high",
    )
    store.save_meta(meta)
    store.task_data_dir(tid).mkdir(parents=True, exist_ok=True)
    (store.task_data_dir(tid) / "brief.md").write_text("do the thing", encoding="utf-8")

    captured = {}

    def fake_spawn(argv, *, log_path, pid_path, cwd, env=None):
        captured["argv"] = argv
        # a codex reviewer emits its verdict as the final agent_message
        log_path.write_text("\n".join([
            _json.dumps({"type": "thread.started", "thread_id": "t1"}),
            _json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                "text": _json.dumps({"findings": [], "verdict": "approve"})}}),
            _json.dumps({"type": "turn.completed",
                         "usage": {"input_tokens": 5, "output_tokens": 2}}),
        ]), encoding="utf-8")
        return 4321

    monkeypatch.setattr(review, "_diff", lambda *a, **k: "")
    monkeypatch.setattr(review.proc, "spawn_detached", fake_spawn)
    monkeypatch.setattr(review, "_wait_for_exit", lambda *a, **k: None)

    out = review.review(tid)
    assert captured["argv"][0] == "codex"  # routed to the codex lane
    assert out.route == "approve"
    # codex reports tokens (no cost) — the review token draw is still recorded
    _, tout, cost = store.cost_total(tid)
    assert tout == 2 and cost is None


# --- narrated activity feed renderer (frugal peek + dashboard brick) ---
def test_summarize_stream_renders_compact_feed(tmp_path):
    import json

    from cox import render

    def asst(*content):
        return json.dumps({"type": "assistant", "message": {"content": list(content)}})

    lines = [
        json.dumps({"type": "system", "subtype": "thinking_tokens"}),  # ignored
        asst({"type": "text", "text": "Installing deps now"}),
        asst({"type": "tool_use", "name": "Bash", "input": {"command": "git rebase origin/main"}}),
        "not json — must be skipped",
        json.dumps(
            {
                "type": "result",
                "is_error": False,
                "total_cost_usd": 1.61,
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 8000,
                    "output_tokens": 42,
                },
            }
        ),
    ]
    log = tmp_path / "worker.log"
    log.write_text("\n".join(lines), encoding="utf-8")

    feed = render.summarize_stream(log, n=15)
    assert feed[0] == "· Installing deps now"
    assert feed[1] == "→ Bash  git rebase origin/main"
    assert feed[2].startswith("■ done  $1.61")
    assert "8100 in / 42 out" in feed[2]
    # no worker log -> friendly line, never a crash
    assert render.summarize_stream(tmp_path / "nope.log") == ["(no worker log yet)"]


# --- codex JSONL renders through the same feed (different schema) ---
def test_summarize_stream_renders_codex_jsonl(tmp_path):
    import json

    from cox import render

    lines = [
        json.dumps({"type": "thread.started", "thread_id": "abc"}),  # skipped
        json.dumps({"type": "turn.started"}),  # skipped
        json.dumps({"type": "item.completed",
                    "item": {"type": "reasoning", "text": "thinking"}}),  # skipped
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "Implemented the feature."}}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "command_execution", "command": "pytest -q"}}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "file_change",
                             "changes": [{"path": "cox/gate.py", "kind": "modified"},
                                         {"path": "cox/fix.py", "kind": "modified"}]}}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 100, "cached_input_tokens": 8000,
                              "output_tokens": 42, "reasoning_output_tokens": 8}}),
    ]
    log = tmp_path / "worker.log"
    log.write_text("\n".join(lines), encoding="utf-8")

    feed = render.summarize_stream(log, n=15)
    assert feed[0] == "· Implemented the feature."
    assert feed[1] == "→ exec  pytest -q"
    assert feed[2] == "→ edit  cox/gate.py (+1)"
    assert feed[3] == "■ done  $? · 8100 in / 50 out"


# --- collapse duplicate adjacent tool lines ---
def test_summarize_stream_collapses_adjacent_duplicates(tmp_path):
    import json

    from cox import render

    def asst(*content):
        return json.dumps({"type": "assistant", "message": {"content": list(content)}})

    tool_line = {"type": "tool_use", "name": "Bash", "input": {"command": "mypy cox/"}}
    lines = [
        asst(tool_line),
        asst(tool_line),
        asst(tool_line),
        asst({"type": "tool_use", "name": "Bash", "input": {"command": "ruff check ."}}),
        asst({"type": "tool_use", "name": "Bash", "input": {"command": "ruff check ."}}),
    ]
    log = tmp_path / "worker.log"
    log.write_text("\n".join(lines), encoding="utf-8")

    feed = render.summarize_stream(log, n=20)
    assert feed[0] == "→ Bash  mypy cox/  (x3)"
    assert feed[1] == "→ Bash  ruff check .  (x2)"
    assert len(feed) == 2


# --- path shortening in tool summaries ---
def test_summarize_stream_shortens_paths(tmp_path):
    import json
    from pathlib import Path as _Path

    from cox import render

    home = str(_Path.home())

    def asst(*content):
        return json.dumps({"type": "assistant", "message": {"content": list(content)}})

    lines = [
        # home-relative path
        asst({"type": "tool_use", "name": "Read",
              "input": {"file_path": f"{home}/myproject/foo.py"}}),
        # cox worktree path -> last 2 segments
        asst({"type": "tool_use", "name": "Edit",
              "input": {"file_path": f"{home}/cox-home/worktrees/task-abc/cox/render.py"}}),
        # cox data path -> last 2 segments
        asst({"type": "tool_use", "name": "Bash",
              "input": {"command": f"cat {home}/cox-home/data/task-abc/evidence/summary.md"}}),
    ]
    log = tmp_path / "worker.log"
    log.write_text("\n".join(lines), encoding="utf-8")

    feed = render.summarize_stream(log, n=20)
    assert feed[0] == "→ Read  ~/myproject/foo.py"
    assert feed[1] == "→ Edit  …/cox/render.py"
    assert "…/evidence/summary.md" in feed[2]


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

"""Reviewer gets read-only worktree access (issue #34).

Run:  cd coxd && COXD_HOME=$(mktemp -d) .venv/bin/python test_review_readonly.py
"""
from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
from pathlib import Path

os.environ.setdefault("COXD_HOME", tempfile.mkdtemp(prefix="coxd-rro-"))

import lane  # noqa: E402
import loop  # noqa: E402
from claude_agent_sdk import ResultMessage  # noqa: E402

wt = Path(tempfile.mkdtemp(prefix="coxd-rro-wt-")).resolve()
(wt / "src").mkdir()
(wt / "src" / "a.py").write_text("x = 1\n")
(wt / ".env.example").write_text("TOKEN=\n")
outside = Path(tempfile.mkdtemp(prefix="coxd-rro-out-")).resolve()
(outside / "note.txt").write_text("hi\n")
(wt / "link").symlink_to(outside)

lane.envscrub.sdk_blanking = lambda: {}  # avoid touching the shared scratch home
hook = lane._make_reviewer_hook(wt)


def denied(tool: str, **ti) -> bool:
    r = asyncio.run(hook({"tool_name": tool, "tool_input": ti}, None, None))
    return bool(r) and r["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- hook: boundary ---
assert not denied("Read", file_path=str(wt / "src" / "a.py"))
assert not denied("Read", file_path="src/a.py")
assert not denied("Read", file_path=str(wt / ".env.example"))
assert denied("Read", file_path=str(wt / ".env"))               # .env read refused
assert denied("Read", file_path=".env")
assert denied("Read", file_path=str(wt / "sub" / ".env.local"))
assert denied("Read", file_path="/etc/passwd")                   # outside worktree
assert denied("Read", file_path=str(wt / "link" / "note.txt"))   # symlink escape
assert denied("Read", file_path=str(wt / "src" / ".." / ".." / "x"))
assert denied("Glob", pattern="../*")
assert denied("Glob", pattern="/etc/*")
assert not denied("Glob", pattern="**/*.py")
assert denied("Grep", pattern="x", path="/etc")
assert not denied("Grep", pattern="x")
(wt / ".env").write_text("TOKEN=fake\n")
assert denied("Grep", pattern="TOKEN")                           # cwd walk would print .env
assert denied("Grep", pattern="x", path=".env")
assert not denied("Grep", pattern="x", path="src")               # narrower path is fine

# --- options passed to query ---
captured: dict = {}


def make_query(msgs=None, exc=None):
    async def fake_query(*, prompt, options):
        captured["options"], captured["prompt"] = options, prompt
        if exc:
            raise exc
        for m in msgs or []:
            yield m
    return fake_query


def run():
    events: list = []
    r = asyncio.run(lane.review("diff", "m", lambda k, d: events.append(k), worktree=wt))
    return r, events


lane.query = make_query(exc=RuntimeError("boom"))
r, ev = run()
o = captured["options"]
assert o.cwd == str(wt)
assert sorted(o.allowed_tools) == ["Glob", "Grep", "Read"]
for t in ("Bash", "Write", "Edit", *lane._WORKER_DENY):
    assert t in o.disallowed_tools, t
assert not set(o.allowed_tools) & set(o.disallowed_tools)
assert o.max_turns == lane._REVIEW_MAX_TURNS and o.max_budget_usd == lane._REVIEW_MAX_BUDGET_USD
assert o.hooks["PreToolUse"], "reviewer hook must be installed"
assert "DATA" in lane._CRITERIA and "NOT a finding" in lane._CRITERIA

# --- review-error: SDK failure and limit hit ---
assert r.outcome == "review-error" and "review-error" in ev
lane.query = make_query(exc=Exception("Reached maximum number of turns"))
r, ev = run()
assert r.outcome == "review-error" and r.verdict is None
err = ResultMessage(subtype="error_max_budget_usd", duration_ms=1, duration_api_ms=1,
                    is_error=True, num_turns=3, session_id="s", total_cost_usd=4.0)
lane.query = make_query(msgs=[err])
r, ev = run()
assert r.outcome == "review-error" and r.verdict is None

# --- callers pass the worktree ---
assert "worktree" in inspect.signature(lane.review).parameters
src = inspect.getsource(loop)
assert src.count("lane.review(") == src.count("worktree=wt)") == 2
print("ok test_review_readonly")

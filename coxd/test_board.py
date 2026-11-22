"""Regression test: /api/tasks payload must build (it crashed after the deploy-cache
refactor left a stale `__defaults__[0].clear()` call).

Standalone (coxd has no pytest harness). Throwaway home:
    cd coxd && COXD_HOME=$(mktemp -d) .venv/bin/python test_board.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-board-")
os.environ.pop("COXD_OWNER_USER", None)
os.environ.pop("COXD_PROXY_SECRET", None)

import board  # noqa: E402
import store  # noqa: E402

store.create_task("t1", "aura-tutor", "brief", "/wt", repo_path="/repo/aura-tutor")
store.set_state("t1", "landed", "merged")

payload = board._tasks_payload()
assert any(r["id"] == "t1" for r in payload["tasks"]), payload
assert all(isinstance(r["timing"], dict) for r in payload["tasks"]), payload
assert payload["tz"]
assert board._tasks_payload() == payload  # second pass uses the TTL cache, no crash
print("ok test_board")

os.environ["COXD_HOST_LABEL"] = "mac"
fp = board._factory_payload()
assert fp["host"] == "mac" and "role" in fp and "paused" in fp, fp
del os.environ["COXD_HOST_LABEL"]
assert board._factory_payload()["host"], "default host falls back to hostname"
assert board._factory_payload()["role"] == "primary"
assert "offline-panel" in board._HTML and "coxd is not running on this machine" in board._HTML
assert "id=foff" in board._HTML and "FACTORY OFF" in board._HTML
assert "coxd-v2" in open(board.__file__).read()
assert "function timingLine" in board._HTML and "data-run=" in board._HTML
assert "waiting_quota:['b-warn'" in board._HTML
assert "t.timing" in board._HTML.split("const k=JSON.stringify")[1][:400]
print("ok test_board factory/offline")

# --- Claude-lane PLAN step (plan_first) --------------------------------------
PLAN5 = ["plan", "implement", "gate", "review", "merge"]


def _row(tid):
    return next(r for r in board._tasks_payload()["tasks"] if r["id"] == tid)


store.create_task("pl1", "aura-tutor", "brief", "/wt", repo_path="/repo/aura-tutor")
store.set_state("pl1", "planning")
store.append_event("pl1", "config", {"planner_model": "claude-opus-4-7"})
r = _row("pl1")
assert r["stages"] == PLAN5 and r["stage"] == {"i": 0, "status": "active"}, r
assert r["plan_md"] is None and r["lane"] == "claude", r
assert r["models"] == {"0": "Opus 4.7"}, r["models"]  # config fallback, no result yet

store.append_event("pl1", "result", {"model": "claude-sonnet-5-5", "cost": 0.12})
store.append_event("pl1", "plan", {"plan_md": "# Plan\n<script>x</script>",
                              "cost": 0.12, "phase": "plan"})
r = _row("pl1")
assert r["stage"] == {"i": 0, "status": "active"} and r["plan_cost"] == 0.12, r
assert r["models"]["0"] == "Sonnet 5.5", r["models"]
store.set_state("pl1", "working")
store.append_event("pl1", "result", {"model": "claude-opus-4-7", "cost": 0.5})
r = _row("pl1")
assert r["stages"] == PLAN5 and r["stage"] == {"i": 1, "status": "active"}, r
assert r["models"] == {"0": "Sonnet 5.5", "1": "Opus 4.7"}, r["models"]
assert r["plan_md"] == "# Plan\n<script>x</script>" and r["plan_cost"] == 0.12, r
assert r["lane"] == "claude"
store.set_state("pl1", "reviewing")
store.append_event("pl1", "result", {"model": "claude-sonnet-5-5", "cost": 0.1})
assert _row("pl1")["stage"]["i"] == 3 and _row("pl1")["models"]["3"] == "Sonnet 5.5"
store.set_state("pl1", "landed", "merged")
assert _row("pl1")["stage"] == {"i": 4, "status": "done"}

# No plan: today's 4 stages, untouched payload shape.
store.create_task("np1", "aura-tutor", "brief", "/wt", repo_path="/repo/aura-tutor")
store.set_state("np1", "working")
store.append_event("np1", "result", {"model": "claude-opus-4-7", "cost": 0.5})
r = _row("np1")
assert r["stages"] is None and r["plan_md"] is None and r["plan_cost"] is None, r
assert r["stage"] == board._stage("working", None)
assert r["models"] == {"0": "Opus 4.7"}, r["models"]

# _plan_info edge cases
ev = lambda k, d: {"kind": k, "data": d, "ts": 1.0}  # noqa: E731
assert board._plan_info([], "working") is None
i = board._plan_info([ev("plan", {"plan_md": "x", "cost": True})], "working")
assert i["plan_cost"] == 0.0 and i["plan_model"] is None, i
i = board._plan_info([ev("plan", {"plan_md": "x", "cost": "n/a"})], "working")
assert i["plan_cost"] == 0.0, i
i = board._plan_info([ev("config", {"planner_model": "claude-sonnet-5-5"}),
                      ev("plan", {"plan_md": "x"})], "working")
assert i["plan_model"] == "Sonnet 5.5", i

# front end: escaped markdown only, cost format, plan step
assert "function planCardHtml" in board._HTML and "mdHtml(t.plan_md)" in board._HTML
assert "(plan $" in board._HTML and "PLAN_TIP" in board._HTML
assert "t.plan_cost" in board._HTML.split("const k=JSON.stringify")[1][:400]
print("ok test_board plan step")

# --- Archived cards: badge, needs_you count, and action gating ----------------
store.create_task("ar1", "aura-tutor", "brief", "/wt", repo_path="/repo/aura-tutor")
store.set_state("ar1", "needs_human", "review-findings")
store.create_task("ar2", "aura-tutor", "brief", "/wt", repo_path="/repo/aura-tutor")
store.set_state("ar2", "needs_human", "gate-red")
store.archive_task("ar1")

def_payload = board._tasks_payload()
assert not any(r["id"] == "ar1" for r in def_payload["tasks"]), def_payload
assert any(r["id"] == "ar2" for r in def_payload["tasks"]), def_payload
assert def_payload["needs_you"] == 1, def_payload

all_payload = board._tasks_payload(show_archived=True)
ar1_row = next(r for r in all_payload["tasks"] if r["id"] == "ar1")
assert ar1_row["archived"] is True, ar1_row
assert ar1_row["needs_you"] is False and ar1_row["active"] is False, ar1_row
assert all_payload["needs_you"] == 1, all_payload

assert "ARCHIVED" in board._HTML and "b-arch" in board._HTML
assert "was: " in board._HTML
retry_seg = board._HTML.split("Stop</button>")[1].split("Approve auto-merge")[0]
assert "!t.archived&&t.state==='needs_human'" in retry_seg
assert "Retry fresh" in retry_seg and "Resume w/ note" in retry_seg
assert "!t.archived&&t.state==='pr_ready'" in retry_seg
assert "!t.archived&&t.active" in board._HTML
assert "!t.archived&&t.approve_auto" in board._HTML
assert "!t.archived&&t.deployable" in board._HTML
assert "t.archived" in board._HTML.split("const k=JSON.stringify")[1][:400]

# Age-archived landed task: no archived_at, updated older than ARCHIVE_DAYS
store.create_task("ar-age", "aura-tutor", "brief", "/wt", repo_path="/repo/aura-tutor")
store.set_state("ar-age", "landed", "merged")
old_ts = time.time() - (store.ARCHIVE_DAYS + 2) * 86400
with store._conn() as c:
    c.execute("UPDATE tasks SET updated=? WHERE id=?", (old_ts, "ar-age"))
age_payload = board._tasks_payload(show_archived=True)
age_row = next(r for r in age_payload["tasks"] if r["id"] == "ar-age")
assert age_row["archived"] is True and age_row["needs_you"] is False, age_row
assert age_row["active"] is False, age_row
print("ok test_board archived")

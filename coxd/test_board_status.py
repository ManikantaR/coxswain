"""Tests for plain-language board status lines, agy cost split, and PLAN tick.

    cd coxd && python test_board_status.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-board-status-")
os.environ.pop("COXD_OWNER_USER", None)
os.environ.pop("COXD_PROXY_SECRET", None)

import board  # noqa: E402
import store  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


# --- 1. Event -> plain-language sentence map ---------------------------------
os.environ["COXD_TZ"] = "America/New_York"
resume_ts = 1794960000.0
expected_hhmm = datetime.fromtimestamp(resume_ts, ZoneInfo("America/New_York")).strftime("%H:%M")

sample_events = [
    ("state", {"state": "pr_ready"}, "Ready to merge"),
    ("state", {"state": "waiting_agy"}, "Waiting for you to run agy"),
    ("state", {"state": "queued"}, "Queued"),
    ("state", {"state": "planning"}, "Writing plan"),
    ("state", {"state": "provisioning"}, "Setting up workspace"),
    ("state", {"state": "working"}, "Implementing changes"),
    ("state", {"state": "gating"}, "Running checks"),
    ("state", {"state": "fixing"}, "Fixing issues"),
    ("state", {"state": "reviewing"}, "Reviewing changes"),
    ("state", {"state": "needs_choice"}, "Waiting for your choice"),
    ("state", {"state": "shipping"}, "Opening pull request"),
    ("state", {"state": "ci-watching"}, "Watching CI checks"),
    ("state", {"state": "waiting_quota"}, "Paused: Claude usage limit"),
    ("state", {"state": "landed"}, "Merged"),
    ("state", {"state": "needs_human"}, "Needs your attention"),
    ("agy-pickup", {"sha": "abc1234"}, "Checking your push"),
    ("agy-head", {"sha": "abc1234"}, "Checking your push"),
    ("agy-applied", {"sha": "abc1234"}, "Loaded your push"),
    ("agy-error", {"stage": "push", "err": "denied"}, "Failed to push agy branch"),
    ("agy-status", {"step": 2, "action": "edit board.py"}, "agy: edit board.py"),
    ("plan", {"plan_md": "# Plan"}, "Plan written"),
    ("review", {"outcome": "reviewed", "verdict": "approve", "findings": []}, "Review approved"),
    ("review", {"outcome": "reviewed", "verdict": "fix", "findings": [{}]},
     "Review requested fixes"),
    ("ci-watch", {"pr_url": "https://example.invalid/1"}, "Watching CI checks"),
    ("ci-triage", {"outcome": "green", "detail": "ok"}, "CI checks passed"),
    ("ci-triage", {"outcome": "needs_human", "detail": "red"}, "CI checks failed"),
    ("fix-round", {"via": "claude", "source": "review"}, "Starting Claude fix round"),
    ("waiting-quota", {"stage": "review", "resume_at": resume_ts},
     f"Paused: Claude usage limit, resumes {expected_hhmm}"),
    ("waiting-quota", {"stage": "review"}, "Paused: Claude usage limit"),
    ("waiting-quota", {"stage": "review", "resume_at": "bad"}, "Paused: Claude usage limit"),
]
for kind, data, want in sample_events:
    got = board.status_line(kind, data)
    check(f"status_line {kind} {data!r}",
          got == want and "{" not in got and "RateLimitInfo" not in got)

# Silent / unknown / malformed inputs must all return ""
silent_cases = [
    ("rate_limit", {"info": "RateLimitInfo(status='allowed', resets_at=123)"}),
    ("created", {"repo": "widgets"}),
    ("result", {"model": "claude-sonnet-5-5", "cost": 0.25}),
    ("config", {"planner_model": "claude-sonnet-5-5"}),
    ("zzz", {"foo": "bar"}),
    ("state", None),
    ("state", "pr_ready"),
    ("waiting-quota", None),
    ("waiting-quota", "not-a-dict"),
    ("plan", None),
    ("agy-pickup", "bad"),
]
for kind, data in silent_cases:
    got = board.status_line(kind, data)
    check(f"silent {kind} {data!r}",
          got == "" and "{" not in got and "RateLimitInfo" not in got)


# --- 2. Fix round numbering & scanning past silent events in `last` ----------
def row_of(tid: str) -> dict:
    return next(r for r in board._tasks_payload()["tasks"] if r["id"] == tid)


store.create_task("agy-fix", "widgets", "brief", "/tmp/wt", lane="agy-handoff")
store.append_event("agy-fix", "plan", {"plan_md": "# Plan"})
store.append_event("agy-fix", "fix-round", {"via": "claude", "source": "review"})
store.append_event("agy-fix", "agy-claude-fix-start", {})
store.append_event("agy-fix", "rate_limit", {"info": "RateLimitInfo(status='allowed')"})
r1 = row_of("agy-fix")
check("last scans past rate_limit and shows round 1",
      r1["last"] == "Claude fixing review findings (round 1)"
      and "RateLimitInfo" not in r1["last"] and "{" not in r1["last"])

store.append_event("agy-fix", "fix-round", {"via": "claude", "source": "review"})
store.append_event("agy-fix", "agy-claude-fix-start", {})
store.append_event("agy-fix", "result", {"cost": 0.1})
r2 = row_of("agy-fix")
check("last shows round 2 after 2 prior fix-round events",
      r2["last"] == "Claude fixing review findings (round 2)")


# --- 3. Cost split & claude_total --------------------------------------------
store.create_task("agy-cost", "widgets", "brief", "/tmp/wt", lane="agy-handoff")
for st_name, cost in [
    ("planning", 0.21),
    ("working", 0.13),
    ("fixing", 0.20),
    ("fixing", 0.30),
    ("reviewing", 1.02),
]:
    store.set_state("agy-cost", st_name)
    store.append_event("agy-cost", "result", {"model": "claude-sonnet-5-5", "cost": cost})
    store.add_cost("agy-cost", cost)

rc = row_of("agy-cost")
sp = rc["claude_split"]
check("claude_split has all 4 phases",
      set(sp) == {"plan", "implement", "fix", "review"})
check("claude_split values match phases",
      abs(sp["plan"] - 0.21) < 1e-9
      and abs(sp["implement"] - 0.13) < 1e-9
      and abs(sp["fix"] - 0.50) < 1e-9
      and abs(sp["review"] - 1.02) < 1e-9)
check("claude_total equals sum of parts and card total",
      abs(rc["claude_total"] - sum(sp.values())) < 1e-9
      and abs(rc["claude_total"] - rc["cost"]) < 1e-9
      and abs(rc["claude_total"] - 1.86) < 1e-9)

# Zero-spend phases: implement and fix are 0.0, JS filters only v > 0
store.create_task("agy-zero", "widgets", "brief", "/tmp/wt", lane="agy-handoff")
store.set_state("agy-zero", "planning")
store.append_event("agy-zero", "result", {"model": "claude-sonnet-5-5", "cost": 0.21})
store.set_state("agy-zero", "reviewing")
store.append_event("agy-zero", "result", {"model": "claude-opus-5", "cost": 1.02})
rz = row_of("agy-zero")
check("zero phases are 0.0 in split",
      rz["claude_split"]["implement"] == 0.0 and rz["claude_split"]["fix"] == 0.0)
check("JS omits zero-spend phases in agyCardHtml",
      "filter(([,v])=>v>0)" in board._HTML and "['agy',sp.implement]" in board._HTML)


# --- 4. PLAN tick (_has_current_plan + _agy_stage) ---------------------------
store.create_task("agy-plan", "widgets", "brief", "/tmp/wt", lane="agy-handoff")
check("queued agy task with no plan -> stage i == 0",
      row_of("agy-plan")["stage"]["i"] == 0)

store.append_event("agy-plan", "plan", {"plan_md": "# Plan 1"})
check("after plan event -> stage i == 1",
      row_of("agy-plan")["stage"]["i"] == 1)

store.append_event("agy-plan", "fix-round", {"via": "agy", "source": "review"})
check("after fix-round with no newer plan -> queued returns to i == 0",
      row_of("agy-plan")["stage"]["i"] == 0)

store.append_event("agy-plan", "plan", {"plan_md": "# Plan 2"})
check("after fix-round followed by new plan -> i == 1",
      row_of("agy-plan")["stage"]["i"] == 1)


# --- 5. TestClient check: /api/tasks + / HTML free of raw dict fragments -----
store.create_task("agy-queued", "widgets", "brief", "/tmp/wt", lane="agy-handoff")
store.append_event("agy-queued", "rate_limit", {"info": "RateLimitInfo(status='allowed')"})

client = TestClient(board.app, base_url="http://localhost:8791")
api_resp = client.get("/api/tasks")
check("/api/tasks returns 200", api_resp.status_code == 200)
q_row = next(r for r in api_resp.json()["tasks"] if r["id"] == "agy-queued")
check("queued agy task last is empty (no created/rate_limit leak)",
      q_row["last"] == "" and q_row["stage"]["i"] == 0)
payload_txt = json.dumps(api_resp.json())
check("no RateLimitInfo or {' in /api/tasks",
      "RateLimitInfo" not in payload_txt and "{'" not in payload_txt)

html_resp = client.get("/")
check("/ returns 200 and serves board HTML",
      html_resp.status_code == 200 and "agyCardHtml" in html_resp.text)
check("queued agy task row has no {' or RateLimitInfo",
      "{'" not in json.dumps(q_row) and "RateLimitInfo" not in json.dumps(q_row))

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)

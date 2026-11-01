"""Standalone test for timeline.derive (pure). Run: python test_timeline.py"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import timeline  # noqa: E402


def st(ts, s):
    return {"kind": "state", "ts": ts, "data": {"state": s, "reason": None}}


def ev(ts, kind, **d):
    return {"kind": kind, "ts": ts, "data": d}


# old task, only state events
t = {"state": "landed", "created": 90}
r = timeline.derive(t, [st(100, "queued"), st(110, "provisioning"), st(120, "working"),
                        st(220, "gating"), st(240, "reviewing"), st(300, "pr_ready"),
                        st(400, "landed")], 1000)
assert r["started_at"] == 110 and r["finished_at"] == 300 and r["landed_at"] == 400, r
assert r["wall_s"] == 190 and r["active_s"] == 190 and not r["in_progress"], r
assert r["human_wait_s"] == 0 and r["merge_wait_s"] == 100 and r["attempts"] == 1, r

# running task: wall grows with now
t = {"state": "working", "created": 0}
evs = [st(10, "queued"), st(20, "working")]
a, b = timeline.derive(t, evs, 100), timeline.derive(t, evs, 200)
assert a["finished_at"] is None and a["in_progress"]
assert a["wall_s"] == 80 and b["wall_s"] == 180 and b["active_s"] == 180

# quota pause
t = {"state": "working", "created": 0}
r = timeline.derive(t, [st(0, "working"), st(50, "waiting_quota"), st(150, "working")], 200)
assert r["paused_quota_s"] == 100 and r["active_s"] == 100, r

# human resume from pr_ready, plus retry
t = {"state": "landed", "created": 0}
r = timeline.derive(t, [st(0, "working"), st(10, "pr_ready"), st(70, "queued"),
                        st(71, "working"), st(100, "landed")], 500)
assert r["human_wait_s"] == 60 and r["attempts"] == 1 and r["finished_at"] == 10, r

# re-review round + gate fix
t = {"state": "pr_ready", "created": 0}
r = timeline.derive(t, [st(0, "working"), ev(5, "fix-trigger", source="gate"),
                        ev(6, "fix-trigger", source="review"),
                        ev(7, "review", **{"pass": "re-review"}), st(9, "pr_ready")], 50)
assert r["review_rounds"] == 1 and r["gate_fixes"] == 1 and r["finished_at"] == 9, r

# CI wait
t = {"state": "landed", "created": 0}
r = timeline.derive(t, [st(0, "working"), st(10, "ci-watching"), st(70, "landed")], 80)
assert r["ci_s"] == 60 and r["active_s"] == 10, r

# no events
r = timeline.derive({"state": "queued", "created": 5}, [], 15)
assert r["started_at"] == 5 and r["wall_s"] == 10, r
print("ok test_timeline")

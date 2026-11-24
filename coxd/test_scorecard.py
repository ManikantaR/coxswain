"""Tests for coxd/scorecard.py (pure helpers + store streak integration)."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scorecard  # noqa: E402
import store  # noqa: E402


def _ev(kind: str, ts: float = 100.0, **data) -> dict:
    return {"kind": kind, "ts": ts, "data": data}


def _row(rows: list[dict], repo: str, lane: str) -> dict:
    return next(r for r in rows if r["repo"] == repo and r["lane"] == lane)


def test_counts_per_repo_lane_and_all() -> None:
    tasks = [
        {"id": "c1", "repo": "acme-widgets", "state": "landed", "lane": None, "cost": 1.5},
        {"id": "c2", "repo": "acme-widgets", "state": "pr_ready", "lane": "claude", "cost": 2.0},
        {"id": "c3", "repo": "acme-widgets", "state": "working", "lane": None, "cost": 0.5},
        {"id": "a1", "repo": "acme-widgets", "state": "landed", "lane": "agy-handoff", "cost": 0.8},
        {"id": "a2", "repo": "acme-widgets", "state": "waiting_agy", "lane": "agy-handoff",
         "cost": 0.2},
        {"id": "b1", "repo": "orbit-gadgets", "state": "needs_human", "lane": None, "cost": 4.0},
        {"id": "b2", "repo": "orbit-gadgets", "state": "needs_choice", "lane": "agy-handoff",
         "cost": 0.3},
    ]
    rows = scorecard.build(tasks, {}, {"acme-widgets": (2, 5), "orbit-gadgets": (0, 3)})

    r_ac = _row(rows, "acme-widgets", "claude")
    assert r_ac["landed"] == 1 and r_ac["needs_you"] == 1 and r_ac["tasks"] == 3
    assert r_ac["streak_current"] == 2 and r_ac["streak_best"] == 5

    r_aa = _row(rows, "acme-widgets", "agy")
    assert r_aa["landed"] == 1 and r_aa["needs_you"] == 1 and r_aa["tasks"] == 2
    assert r_aa["median_cost"] is None and r_aa["total_cost"] is None
    assert r_aa["streak_current"] == 2 and r_aa["streak_best"] == 5

    r_oc = _row(rows, "orbit-gadgets", "claude")
    assert r_oc["landed"] == 0 and r_oc["needs_you"] == 1
    assert r_oc["streak_current"] == 0 and r_oc["streak_best"] == 3

    r_oa = _row(rows, "orbit-gadgets", "agy")
    assert r_oa["landed"] == 0 and r_oa["needs_you"] == 1

    r_all = _row(rows, "all", "all")
    assert r_all["is_total"] is True
    assert r_all["landed"] == 2 and r_all["needs_you"] == 4 and r_all["tasks"] == 7
    assert r_all["streak_current"] is None and r_all["streak_best"] is None


def test_first_review_and_first_gate_pass_pct() -> None:
    tasks = [
        # Gate passed first, review approved first (with low advisory finding)
        {"id": "t1", "repo": "acme-widgets", "state": "landed", "cost": 1.0},
        # Gate failed first then passed; review failed first then approved after fix
        {"id": "t2", "repo": "acme-widgets", "state": "landed", "cost": 2.0},
        # Gate passed first; review rejected
        {"id": "t3", "repo": "acme-widgets", "state": "needs_human", "cost": 3.0},
        # Never reached gate or review (excluded from both denominators)
        {"id": "t4", "repo": "acme-widgets", "state": "working", "cost": 0.5},
    ]
    evs = {
        "t1": [
            _ev("gate", 10, passed=True),
            _ev("review", 20, outcome="reviewed", verdict="approve",
                findings=[{"severity": "low", "pillar": "advisory", "summary": "nit"}]),
        ],
        "t2": [
            _ev("gate", 10, passed=False),
            _ev("gate", 15, passed=True),
            _ev("review", 20, outcome="reviewed", verdict="fix",
                findings=[{"severity": "high", "pillar": "correctness", "summary": "bug"}]),
            _ev("review", 30, outcome="reviewed", verdict="approve", findings=[]),
        ],
        "t3": [
            _ev("gate", 10, passed=True),
            _ev("review", 20, outcome="reviewed", verdict="reject",
                findings=[{"severity": "high", "pillar": "security", "summary": "bad"}]),
        ],
        "t4": [_ev("state", 5, state="working")],
    }
    rows = scorecard.build(tasks, evs, {})
    r = _row(rows, "acme-widgets", "claude")
    # 1 of 3 passed first review (t1 only; t2 failed first even though it later passed)
    assert r["first_review_passed"] == 1 and r["first_review_total"] == 3
    assert abs(r["first_review_pct"] - 33.3) < 1e-9
    # 2 of 3 passed first gate (t1, t3; t2 failed first even though retry passed)
    assert r["first_gate_passed"] == 2 and r["first_gate_total"] == 3
    assert abs(r["first_gate_pct"] - 66.7) < 1e-9

    # Parallel list form of events_by_task also works; empty reaches give None
    empty_rows = scorecard.build(
        [{"id": "e1", "repo": "zeta-gizmos", "state": "queued", "cost": 0.0}],
        [[]],
        {},
    )
    rz = _row(empty_rows, "zeta-gizmos", "claude")
    assert rz["first_review_pct"] is None and rz["first_gate_pct"] is None


def test_median_and_total_cost() -> None:
    # Odd count (3 tasks: 1.0, 2.0, 9.0 -> median 2.0, total 12.0)
    odd_tasks = [
        {"id": "o1", "repo": "odd-repo", "state": "landed", "cost": 9.0},
        {"id": "o2", "repo": "odd-repo", "state": "landed", "cost": 1.0},
        {"id": "o3", "repo": "odd-repo", "state": "landed", "cost": 2.0},
    ]
    # Even count (4 tasks: 1.0, 2.0, 4.0, 9.0 -> median 3.0, total 16.0)
    even_tasks = [
        {"id": "e1", "repo": "even-repo", "state": "landed", "cost": 1.0},
        {"id": "e2", "repo": "even-repo", "state": "landed", "cost": 4.0},
        {"id": "e3", "repo": "even-repo", "state": "landed", "cost": 2.0},
        {"id": "e4", "repo": "even-repo", "state": "landed", "cost": 9.0},
    ]
    # Repo with only zero-cost / unstarted task -> None
    zero_tasks = [
        {"id": "z1", "repo": "zero-repo", "state": "queued", "cost": 0.0},
    ]
    rows = scorecard.build(odd_tasks + even_tasks + zero_tasks, {}, {})
    r_odd = _row(rows, "odd-repo", "claude")
    assert r_odd["median_cost"] == 2.0 and r_odd["total_cost"] == 12.0

    r_even = _row(rows, "even-repo", "claude")
    assert r_even["median_cost"] == 3.0 and r_even["total_cost"] == 16.0

    r_zero = _row(rows, "zero-repo", "claude")
    assert r_zero["median_cost"] is None and r_zero["total_cost"] is None

    # Completely empty tasks list -> all-repos row returns None, no crash
    empty = scorecard.build([], {}, {})
    r_all = _row(empty, "all", "all")
    assert r_all["median_cost"] is None and r_all["total_cost"] is None
    assert r_all["first_review_pct"] is None and r_all["first_gate_pct"] is None


def test_streaks_from_store_record_merge() -> None:
    prev = os.environ.get("COXD_HOME")
    os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-scorecard-")
    try:
        repo = "acme-widgets"
        for i in range(3):
            store.create_task(f"s{i}", repo, "brief", "/tmp/wt")
            store.record_merge(repo, True, task_id=f"s{i}")
        assert store.streaks()[repo] == (3, 3)

        store.create_task("s_bad", repo, "brief", "/tmp/wt")
        store.record_merge(repo, False, task_id="s_bad")
        assert store.streaks()[repo] == (0, 3)
        assert store.best_streaks()[repo] == 3

        store.create_task("s_next", repo, "brief", "/tmp/wt")
        store.record_merge(repo, True, task_id="s_next")
        assert store.streaks()[repo] == (1, 3)

        rows = scorecard.build(store.list_tasks(), {}, store.streaks())
        r = _row(rows, repo, "claude")
        assert r["streak_current"] == 1 and r["streak_best"] == 3
    finally:
        if prev is None:
            os.environ.pop("COXD_HOME", None)
        else:
            os.environ["COXD_HOME"] = prev


def test_weekly_cost_per_landed() -> None:
    tz = "UTC"
    # Anchor on Wednesday 2026-06-10 12:00 UTC (Monday of that week is 2026-06-08)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=ZoneInfo(tz)).timestamp()
    week = 7 * 86400

    tasks = [
        # Current week (bucket 7): 2 Claude landed tasks ($2 + $4 -> $3.0)
        {"id": "w0a", "repo": "r", "state": "landed", "cost": 2.0},
        {"id": "w0b", "repo": "r", "state": "landed", "cost": 4.0},
        # Current week agy task: excluded from Claude cost/landed
        {"id": "w0agy", "repo": "r", "state": "landed", "lane": "agy-handoff", "cost": 0.5},
        # 2 weeks ago (bucket 5): 1 Claude landed task ($1.5)
        {"id": "w2", "repo": "r", "state": "landed", "cost": 1.5},
        # 7 weeks ago (bucket 0, oldest): 1 Claude landed task ($5.0)
        {"id": "w7", "repo": "r", "state": "landed", "cost": 5.0},
        # 8 weeks ago (out of 8-week window): ignored
        {"id": "w8", "repo": "r", "state": "landed", "cost": 99.0},
        # Not landed: ignored
        {"id": "wip", "repo": "r", "state": "working", "cost": 10.0},
    ]
    evs = {
        "w0a": [_ev("state", now - 3600, state="landed")],
        "w0b": [_ev("state", now - 7200, state="landed")],
        "w0agy": [_ev("state", now - 1800, state="landed")],
        "w2": [_ev("state", now - 2 * week, state="landed")],
        "w7": [_ev("state", now - 7 * week, state="landed")],
        "w8": [_ev("state", now - 8 * week, state="landed")],
        "wip": [_ev("state", now - 100, state="working")],
    }
    trend = scorecard.weekly_cost_per_landed(tasks, evs, now, tz, weeks=8)
    assert len(trend) == 8, trend
    assert trend == [5.0, None, None, None, None, 1.5, None, 3.0], trend

    # Empty tasks -> 8 Nones
    assert scorecard.weekly_cost_per_landed([], {}, now, tz, weeks=8) == [None] * 8


def test_over_budget() -> None:
    assert scorecard.over_budget(3.01, 3.0) is True
    assert scorecard.over_budget(3.0, 3.0) is False
    assert scorecard.over_budget(2.99, 3.0) is False
    assert scorecard.over_budget(None, 3.0) is False
    assert scorecard.over_budget(5.0, 0.0) is False
    assert scorecard.over_budget(True, 0.5) is False


if __name__ == "__main__":
    test_counts_per_repo_lane_and_all()
    test_first_review_and_first_gate_pass_pct()
    test_median_and_total_cost()
    test_streaks_from_store_record_merge()
    test_weekly_cost_per_landed()
    test_over_budget()
    print("ok test_scorecard")

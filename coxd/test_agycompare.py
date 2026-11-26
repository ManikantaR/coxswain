"""Tests for coxd/agycompare.py (pure comparison helpers + store size column)."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agycompare  # noqa: E402
import store  # noqa: E402


def _ev(kind: str, ts: float = 100.0, **data) -> dict:
    return {"kind": kind, "ts": ts, "data": data}


def _group(res: dict, lane: str, size: str) -> dict:
    return next(g for g in res["groups"] if g["lane"] == lane and g["size"] == size)


def test_task_record_full_events() -> None:
    task = {
        "id": "task-agy-s1",
        "repo": "repo-test",
        "lane": "agy-handoff",
        "size": "small",
        "state": "landed",
        "cost": 9.99,  # agy lane must ignore tasks.cost and use plan+review only
        "created": 1000.0,
    }
    evs = [
        _ev("state", 1000.0, state="planning"),
        _ev("result", 1010.0, model="claude-sonnet-5-5", cost=0.15),
        # First notify -> push interval: 1020 -> 1140 = 120s (2.0 min)
        _ev("state", 1020.0, state="waiting_agy"),
        _ev("state", 1140.0, state="gating"),
        _ev("agy-pickup", 1141.0, sha="aaa1111", agy_tokens=4200),
        _ev("gate", 1150.0, passed=False, failing="test"),
        _ev("fix-round", 1160.0, via="agy", source="gate"),
        # Second notify -> push interval: 1170 -> 1350 = 180s (3.0 min)
        _ev("state", 1170.0, state="waiting_agy"),
        _ev("state", 1350.0, state="gating"),
        _ev("agy-pickup", 1351.0, sha="bbb2222", agy_tokens=5600),
        _ev("gate", 1360.0, passed=True),
        _ev("state", 1370.0, state="reviewing"),
        _ev("result", 1380.0, model="claude-opus-4-7", cost=0.45),
        _ev(
            "review",
            1381.0,
            outcome="reviewed",
            verdict="fix",
            findings=[
                {"severity": "critical", "pillar": "correctness", "summary": "bug"},
                {"severity": "major", "pillar": "security", "summary": "leak"},
                {"severity": "med", "pillar": "contract", "summary": "edge"},
            ],
        ),
        _ev("fix-round", 1390.0, via="claude", source="review"),
        _ev("state", 1395.0, state="fixing"),
        _ev("result", 1410.0, model="claude-sonnet-5-5", cost=0.80),  # fix cost excluded for agy
        _ev("state", 1420.0, state="reviewing"),
        _ev("result", 1430.0, model="claude-opus-4-7", cost=0.25),
        _ev(
            "review",
            1431.0,
            outcome="reviewed",
            verdict="approve",
            findings=[{"severity": "low", "pillar": "advisory", "summary": "nit"}],
        ),
        _ev("state", 1500.0, state="landed"),
    ]
    rec = agycompare.task_record(task, evs)
    assert rec["lane"] == "agy"
    assert rec["size"] == "small"
    assert rec["gate_first_pass"] is False
    assert rec["gate_reds"] == 1
    assert rec["fix_rounds_agy"] == 1
    assert rec["fix_rounds_claude"] == 1
    assert rec["findings"] == {"critical": 1, "major": 2, "minor": 1}
    assert abs(rec["plan_cost"] - 0.15) < 1e-9
    assert abs(rec["review_cost"] - 0.70) < 1e-9
    assert abs(rec["claude_cost"] - 0.85) < 1e-9
    # 120s + 180s = 300s = 5.0 minutes exactly
    assert rec["human_minutes"] == 5.0
    assert rec["agy_tokens"] == 5600
    assert rec["outcome"] == "merged"
    # 2 fix rounds (>1) -> needed_fixes even though ultimately approved
    assert rec["result"] == "needed_fixes"


def test_task_record_outcomes_open_interval_and_cost_lanes() -> None:
    # Open needs-you interval closes at last event timestamp (deterministic, no "now")
    open_task = {
        "id": "task-agy-open",
        "repo": "repo-test",
        "lane": "agy-handoff",
        "size": "small",
        "state": "waiting_agy",
        "cost": 0.5,
    }
    open_evs = [
        _ev("state", 100.0, state="waiting_agy"),
        _ev("agy-status", 250.0, action="editing"),
    ]
    rec_open = agycompare.task_record(open_task, open_evs)
    assert rec_open["outcome"] == "open"
    assert rec_open["human_minutes"] == 2.5
    assert rec_open["result"] is None

    # Closed via failed vs archived; non-agy lane uses tasks.cost
    failed_task = {
        "id": "task-claude-fail",
        "repo": "repo-test",
        "lane": None,
        "size": "medium",
        "state": "failed",
        "cost": 3.75,
    }
    rec_fail = agycompare.task_record(
        failed_task,
        [
            _ev("state", 10.0, state="planning"),
            _ev("result", 20.0, cost=0.10),
        ],
    )
    assert rec_fail["lane"] == "claude"
    assert rec_fail["claude_cost"] == 3.75
    assert rec_fail["outcome"] == "closed"
    assert rec_fail["result"] == "needs_human"

    archived_task = {
        "id": "task-agy-arch",
        "repo": "repo-test",
        "lane": "agy-handoff",
        "size": "small",
        "state": "needs_human",
        "archived_at": 500.0,
        "cost": 1.2,
    }
    rec_arch = agycompare.task_record(archived_task, [])
    assert rec_arch["outcome"] == "closed"
    assert rec_arch["result"] == "needs_human"

    # *-cap stop forces needs_human result even if state is landed
    cap_task = {
        "id": "task-agy-cap",
        "repo": "repo-test",
        "lane": "agy-handoff",
        "size": "small",
        "state": "landed",
    }
    rec_cap = agycompare.task_record(
        cap_task,
        [
            _ev("review-cap", 50.0, cap=3),
            _ev("review", 60.0, outcome="reviewed", verdict="approve", findings=[]),
        ],
    )
    assert rec_cap["result"] == "needs_human"


def test_build_groups_metrics_and_sparkline() -> None:
    tasks: list[dict] = []
    evs: dict[str, list[dict]] = {}

    # 12 agy+small tasks to verify sparkline ordering (created asc) and cap of 10
    pattern = [
        "needs_human", "needs_human",  # oldest 2 -> dropped by 10-item cap
        "approved", "needed_fixes", "approved", "needs_human", "approved",
        "approved", "needed_fixes", "approved", "approved", "needs_human",
    ]
    for i, kind in enumerate(pattern):
        tid = f"task-agy-s{i}"
        state = "needs_human" if kind == "needs_human" else "landed"
        tasks.append({
            "id": tid,
            "repo": "repo-test",
            "lane": "agy-handoff",
            "size": "small",
            "state": state,
            "cost": 99.0,
            "created": 100.0 + i * 10.0,
        })
        t_evs = [
            _ev("state", 10.0, state="planning"),
            _ev("result", 11.0, cost=0.10),
            _ev("state", 20.0, state="waiting_agy"),
            _ev("state", 80.0, state="gating"),  # 1.0 min
            _ev("gate", 85.0, passed=(i % 2 == 0)),
            _ev("state", 90.0, state="reviewing"),
            _ev("result", 91.0, cost=0.20),
        ]
        if kind == "approved":
            t_evs.append(_ev("review", 95.0, outcome="reviewed", verdict="approve", findings=[]))
        elif kind == "needed_fixes":
            t_evs.append(_ev("fix-round", 92.0, via="agy", source="review"))
            t_evs.append(_ev("fix-round", 93.0, via="claude", source="review"))
            t_evs.append(_ev("review", 95.0, outcome="reviewed", verdict="approve", findings=[]))
        else:
            t_evs.append(
                _ev(
                    "review",
                    95.0,
                    outcome="reviewed",
                    verdict="reject",
                    findings=[{"severity": "high", "pillar": "correctness", "summary": "err"}],
                )
            )
        evs[tid] = t_evs

    # 2 claude+small tasks
    tasks.extend([
        {
            "id": "task-cl-s1",
            "repo": "repo-test",
            "lane": None,
            "size": "small",
            "state": "landed",
            "cost": 2.0,
            "created": 50.0,
        },
        {
            "id": "task-cl-s2",
            "repo": "repo-test",
            "lane": "claude",
            "size": "small",
            "state": "landed",
            "cost": 4.0,
            "created": 60.0,
        },
    ])
    evs["task-cl-s1"] = [
        _ev("gate", 10.0, passed=True),
        _ev("review", 20.0, outcome="reviewed", verdict="approve", findings=[]),
        _ev("state", 30.0, state="pr_ready"),
        _ev("state", 90.0, state="landed"),  # 1.0 min
    ]
    evs["task-cl-s2"] = [
        _ev("gate", 10.0, passed=False),
        _ev("fix-round", 15.0, via="claude", source="gate"),
        _ev(
            "review",
            20.0,
            outcome="reviewed",
            verdict="fix",
            findings=[
                {"severity": "critical", "pillar": "correctness", "summary": "a"},
                {"severity": "major", "pillar": "security", "summary": "b"},
            ],
        ),
        _ev("review", 25.0, outcome="reviewed", verdict="approve", findings=[]),
        _ev("state", 30.0, state="pr_ready"),
        _ev("state", 210.0, state="landed"),  # 3.0 min
    ]

    # 1 claude+medium, 1 agy+medium, 1 unsized
    tasks.extend([
        {
            "id": "task-cl-m1",
            "repo": "repo-test",
            "lane": None,
            "size": "medium",
            "state": "landed",
            "cost": 5.0,
            "created": 70.0,
        },
        {
            "id": "task-agy-m1",
            "repo": "repo-test",
            "lane": "agy-handoff",
            "size": "medium",
            "state": "landed",
            "cost": 8.0,
            "created": 80.0,
        },
        {
            "id": "task-unsized",
            "repo": "repo-test",
            "lane": None,
            "size": None,
            "state": "landed",
            "cost": 1.0,
            "created": 90.0,
        },
    ])
    evs["task-cl-m1"] = [
        _ev("gate", 10.0, passed=True),
        _ev("review", 20.0, outcome="reviewed", verdict="approve", findings=[]),
    ]
    evs["task-agy-m1"] = [
        _ev("state", 5.0, state="planning"),
        _ev("result", 6.0, cost=0.25),
        _ev("gate", 10.0, passed=True),
        _ev("state", 15.0, state="reviewing"),
        _ev("result", 16.0, cost=0.50),
        _ev("review", 20.0, outcome="reviewed", verdict="approve", findings=[]),
    ]
    evs["task-unsized"] = []

    res = agycompare.build(tasks, evs)
    assert res["unsized"] == 1
    assert len(res["groups"]) == 4

    g_as = _group(res, "agy", "small")
    assert g_as["tasks"] == 12
    assert g_as["first_pass_pct"] == 50.0
    assert g_as["median_claude_usd"] == 0.3
    assert g_as["median_human_min"] == 1.0
    assert len(g_as["spark"]) == 10
    assert g_as["spark"] == pattern[-10:]

    g_cs = _group(res, "claude", "small")
    assert g_cs["tasks"] == 2
    assert g_cs["first_pass_pct"] == 50.0
    assert g_cs["median_fix_rounds"] == 0.5
    assert g_cs["crit_major_per_task"] == 1.0  # 2 crit+major across 2 tasks
    assert g_cs["median_claude_usd"] == 3.0
    assert g_cs["median_human_min"] == 2.0
    assert g_cs["spark"] == ["approved", "approved"]

    g_cm = _group(res, "claude", "medium")
    assert g_cm["tasks"] == 1 and g_cm["median_claude_usd"] == 5.0

    g_am = _group(res, "agy", "medium")
    assert g_am["tasks"] == 1 and g_am["median_claude_usd"] == 0.75


def _make_agy_small(
    tid: str,
    created: float,
    *,
    approved: bool = True,
    fixes: int = 0,
    state: str = "landed",
    size: str | None = "small",
    lane: str | None = "agy-handoff",
) -> tuple[dict, list[dict]]:
    t = {
        "id": tid,
        "repo": "repo-test",
        "lane": lane,
        "size": size,
        "state": state,
        "cost": 1.0,
        "created": created,
    }
    e = [_ev("fix-round", 10.0 + k, via="claude", source="review") for k in range(fixes)]
    if approved:
        e.append(_ev("review", 50.0, outcome="reviewed", verdict="approve", findings=[]))
    else:
        e.append(
            _ev(
                "review",
                50.0,
                outcome="reviewed",
                verdict="reject",
                findings=[{"severity": "high", "pillar": "correctness", "summary": "x"}],
            )
        )
    return t, e


def test_graduation_hint_cases() -> None:
    # 4 finished agy+small + medium + unsized + open + claude -> still collecting 4/5
    pairs = [
        _make_agy_small(f"task-agy-s{i}", 10.0 + i) for i in range(4)
    ] + [
        _make_agy_small("task-agy-m1", 20.0, size="medium"),
        _make_agy_small("task-agy-u1", 21.0, size=None),
        _make_agy_small("task-agy-open", 22.0, state="waiting_agy"),
        _make_agy_small("task-cl-s1", 23.0, lane=None),
    ]
    res4 = agycompare.build([t for t, _ in pairs], {t["id"]: e for t, e in pairs})
    assert res4["hint"] == {"state": "collecting", "n": 4, "need": 5}

    # 4/5 approved within 1 round (including 1 task with 1 fix round) -> ready with exact text
    ready_pairs = [
        _make_agy_small("task-agy-r1", 1.0, approved=True, fixes=0),
        _make_agy_small("task-agy-r2", 2.0, approved=True, fixes=1),
        _make_agy_small("task-agy-r3", 3.0, approved=True, fixes=0),
        _make_agy_small("task-agy-r4", 4.0, approved=True, fixes=1),
        _make_agy_small("task-agy-r5", 5.0, approved=True, fixes=2),  # >1 fix round -> not counted
    ]
    res_ready = agycompare.build(
        [t for t, _ in ready_pairs],
        {t["id"]: e for t, e in ready_pairs},
    )
    assert res_ready["hint"] == {
        "state": "ready",
        "approved": 4,
        "of": 5,
        "text": "small: 4/5 approved within 1 round, ready for medium",
    }

    # 3/5 approved within 1 round -> not_yet
    not_yet_pairs = [
        _make_agy_small("task-agy-n1", 1.0, approved=True, fixes=0),
        _make_agy_small("task-agy-n2", 2.0, approved=True, fixes=1),
        _make_agy_small("task-agy-n3", 3.0, approved=True, fixes=0),
        _make_agy_small("task-agy-n4", 4.0, approved=True, fixes=2),
        _make_agy_small("task-agy-n5", 5.0, approved=False, state="failed"),
    ]
    res_ny = agycompare.build(
        [t for t, _ in not_yet_pairs],
        {t["id"]: e for t, e in not_yet_pairs},
    )
    assert res_ny["hint"]["state"] == "not_yet"
    assert res_ny["hint"]["approved"] == 3
    assert res_ny["hint"]["of"] == 5

    # Latest 5 window: oldest 2 failed, latest 5 have 4 approved -> ready
    latest_pairs = [
        _make_agy_small("task-agy-old1", 1.0, approved=False, state="failed"),
        _make_agy_small("task-agy-old2", 2.0, approved=False, state="failed"),
        *ready_pairs,
    ]
    # Shift ready_pairs timestamps after old1/old2
    for idx, (t, _) in enumerate(latest_pairs[2:], start=3):
        t["created"] = float(idx)
    res_latest = agycompare.build(
        [t for t, _ in latest_pairs],
        {t["id"]: e for t, e in latest_pairs},
    )
    assert res_latest["hint"]["state"] == "ready"
    assert res_latest["hint"]["approved"] == 4


def test_zero_agy_tasks() -> None:
    empty = agycompare.build([], {})
    assert empty["hint"] == {"state": "collecting", "n": 0, "need": 5}
    assert empty["unsized"] == 0
    for g in empty["groups"]:
        assert g["tasks"] == 0
        assert g["first_pass_pct"] is None
        assert g["median_fix_rounds"] is None
        assert g["crit_major_per_task"] is None
        assert g["median_claude_usd"] is None
        assert g["median_human_min"] is None
        assert g["spark"] == []

    claude_only = agycompare.build(
        [
            {
                "id": "task-cl-only",
                "repo": "repo-test",
                "lane": None,
                "size": "small",
                "state": "landed",
                "cost": 1.25,
                "created": 10.0,
            }
        ],
        {"task-cl-only": [_ev("gate", 10.0, passed=True)]},
    )
    assert claude_only["hint"] == {"state": "collecting", "n": 0, "need": 5}
    for sz in ("small", "medium"):
        ag = _group(claude_only, "agy", sz)
        assert ag["tasks"] == 0
        assert ag["first_pass_pct"] is None
        assert ag["median_claude_usd"] is None
        assert ag["spark"] == []


def test_store_size_column() -> None:
    prev = os.environ.get("COXD_HOME")
    os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-agycmp-")
    try:
        store.create_task("task-s1", "repo-test", "brief", "/tmp/wt", size="small")
        store.create_task("task-m1", "repo-test", "brief", "/tmp/wt", lane="agy-handoff",
                          size="medium")
        store.create_task("task-none", "repo-test", "brief", "/tmp/wt")
        assert store.get_task("task-s1")["size"] == "small"
        assert store.get_task("task-m1")["size"] == "medium"
        assert store.get_task("task-none")["size"] is None
    finally:
        if prev is None:
            os.environ.pop("COXD_HOME", None)
        else:
            os.environ["COXD_HOME"] = prev


if __name__ == "__main__":
    test_task_record_full_events()
    test_task_record_outcomes_open_interval_and_cost_lanes()
    test_build_groups_metrics_and_sparkline()
    test_graduation_hint_cases()
    test_zero_agy_tasks()
    test_store_size_column()
    print("ok test_agycompare")

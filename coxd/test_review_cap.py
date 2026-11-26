"""Review-round cap: after MAX_REVIEW_ROUNDS completed reviews, no further review starts;
the task goes needs_human/review-cap with the open findings instead of looping.

Standalone: cd coxd && COXD_HOME=$(mktemp -d) python test_review_cap.py   (also runs under pytest)
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
tempfile.tempdir = tempfile.mkdtemp(prefix="coxd-rc-tmp-")
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-rc-")

import gate  # noqa: E402
import lane  # noqa: E402
import loop  # noqa: E402
import notify  # noqa: E402
import store  # noqa: E402
import triage  # noqa: E402

shipped: list[str] = []
gate.run_gate = lambda wt, entry: {"passed": True, "steps": {"test": "ok"}}  # type: ignore
gate.head_sha = lambda wt: "a" * 40  # type: ignore
gate.review_diff = lambda wt, entry=None, base=None, budget=0: "FULL_DIFF"  # type: ignore
gate.rereview_input = lambda wt, entry, since, prev, budget=0: "DELTA"  # type: ignore
notify.notify_async = lambda *a, **k: None  # type: ignore
sys.modules["ship"] = SimpleNamespace(ship=lambda tid: (shipped.append(tid) or ("local", None)))


async def fake_worker(*a, **k):
    return SimpleNamespace(cost=0.0, summary="fixed", session_id="s1", quota_reset_at=None,
                           is_error=False)

lane.run_worker = fake_worker  # type: ignore

FINDING = [{"severity": "high", "message": "open finding F"}]


def run(tid, prior_reviews, verdicts):
    """Seed `prior_reviews` completed reviews, then run the tail with `verdicts`."""
    seq = iter(verdicts)
    calls: list[str] = []

    async def fake_review(diff, model, emit, effort=None, worktree=None):
        calls.append(diff)
        return SimpleNamespace(outcome="reviewed", verdict=next(seq), cost=0.0,
                               quota_reset_at=None, findings=list(FINDING))

    lane.review = fake_review  # type: ignore
    store.create_task(tid, "example", "brief", "/wt", repo_path="/repo/example")
    store.set_session(tid, "s0")
    for i in range(prior_reviews):
        store.append_event(tid, "review", {"outcome": "reviewed", "verdict": "fix",
                                           "findings": FINDING, "head": f"{i:040x}"})
    store.set_review(tid, "fix", FINDING)
    state = asyncio.run(loop._gate_review_ship(
        tid, Path("/wt"), "example", {}, "w", "rev-m", "low",
        lambda k, d: store.append_event(tid, k, d)))
    evs = store.events(tid)
    return state, calls, evs


def test_fourth_review_is_not_started():
    state, calls, evs = run("cap3", 3, [])
    assert calls == [], "a 4th review must not be started"
    t = store.get_task("cap3")
    assert state == "needs_human" and t["reason"] == "review-cap"
    cap = [e["data"] for e in evs if e["kind"] == "review-cap"]
    assert len(cap) == 1 and cap[0]["rounds"] == 3 and cap[0]["cap"] == loop.MAX_REVIEW_ROUNDS
    assert cap[0]["findings"] == FINDING
    assert "open finding F" in triage.next_step_line(t)
    assert shipped == []


def test_third_review_still_runs_after_two():
    state, calls, evs = run("cap2", 2, ["approve"])
    assert len(calls) == 1 and state == "pr_ready"
    assert not [e for e in evs if e["kind"] == "review-cap"]


def test_third_review_that_still_asks_for_fixes_stops_without_a_fix_round():
    # 2 prior reviews; the 3rd comes back `fix` -> cap, not a (fourth-review-bound) fix round
    state, calls, evs = run("cap2fix", 2, ["fix"])
    assert len(calls) == 1 and state == "needs_human"
    assert store.get_task("cap2fix")["reason"] == "review-cap"
    assert not [e for e in evs if e["kind"] == "fix-trigger"]


def test_fix_round_then_rereview_ends_at_cap_when_still_failing():
    # fresh task: review1 fix -> fix -> review2 fix -> (rounds 2 < 3) review3?? only 2 here
    state, calls, evs = run("capfresh", 0, ["fix", "fix"])
    assert len(calls) == 2 and state == "needs_human"
    assert store.get_task("capfresh")["reason"] == "review-findings"  # under the cap: unchanged


def test_error_reviews_do_not_count_and_resume_after_cap_gets_new_allowance():
    store.create_task("capx", "example", "b", "/wt")
    store.append_event("capx", "review", {"outcome": "review-error"})
    for _ in range(3):
        store.append_event("capx", "review", {"outcome": "reviewed", "verdict": "fix"})
    assert loop._review_rounds("capx") == 3
    store.append_event("capx", "review-cap", {})
    assert loop._review_rounds("capx") == 0 and not loop._review_capped("capx")


def test_approved_latest_review_is_not_capped_on_resume():
    # fix, fix, (resume), approve -> 3 reviews, but the last approved: shipping resumes, no cap
    store.create_task("capok", "example", "brief", "/wt", repo_path="/repo/example")
    for v in ("fix", "fix", "approve"):
        store.append_event("capok", "review", {"outcome": "reviewed", "verdict": v})
    assert loop._review_rounds("capok") == 3 and not loop._review_capped("capok")
    store.append_event("capok", "review", {"outcome": "reviewed", "verdict": "fix"})
    assert loop._review_capped("capok")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)

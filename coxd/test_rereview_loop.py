"""First review = full diff on the review role; a review after a fix round = delta +
previous findings on the rereview role; every review event records the reviewed head.
Only a fresh `approve` ships."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-rrl-")

import gate  # noqa: E402
import lane  # noqa: E402
import loop  # noqa: E402
import notify  # noqa: E402
import store  # noqa: E402

shipped: list[str] = []
heads = iter(f"{i:040x}" for i in range(1, 100))
gate.run_gate = lambda wt, entry: {"passed": True, "steps": {"test": "ok"}}  # type: ignore
gate.head_sha = lambda wt: next(heads)  # type: ignore
gate.review_diff = lambda wt, entry=None, base=None, budget=0: "FULL_DIFF"  # type: ignore
gate.rereview_input = lambda wt, entry, since, prev, budget=0: (  # type: ignore
    f"DELTA since={since} prev={prev[0]['message']}")
notify.notify_async = lambda *a, **k: None  # type: ignore
sys.modules["ship"] = SimpleNamespace(ship=lambda tid: (shipped.append(tid) or ("local", None)))


async def fake_worker(*a, **k):
    return SimpleNamespace(cost=0.0, summary="fixed", session_id="s1", quota_reset_at=None,
                           is_error=False)

lane.run_worker = fake_worker  # type: ignore


def run(tid, verdicts):
    seq = iter(verdicts)
    calls = []

    async def fake_review(diff, model, emit, effort=None, worktree=None):
        calls.append((diff, model, effort))
        return SimpleNamespace(outcome="reviewed", verdict=next(seq), cost=0.0,
                               quota_reset_at=None,
                               findings=[{"severity": "high", "message": f"F{len(calls)}"}])

    lane.review = fake_review  # type: ignore
    store.create_task(tid, "example", "brief", "/wt", repo_path="/repo/example")
    store.set_session(tid, "s0")
    state = asyncio.run(loop._gate_review_ship(
        tid, Path("/wt"), "example", {}, "w", "rev-m", "low",
        lambda k, d: store.append_event(tid, k, d), review_effort="medium",
        rereview_model="rerev-m", rereview_effort="low"))
    evs = [e["data"] for e in store.events(tid) if e["kind"] == "review"]
    return state, calls, evs


shipped.clear()
state, calls, evs = run("t1", ["fix", "approve"])
assert state == "pr_ready" and len(shipped) == 1
assert calls[0] == ("FULL_DIFF", "rev-m", "medium")
h1, h2 = evs[0]["head"], evs[1]["head"]
assert h1 and h2 and h1 != h2
assert calls[1] == (f"DELTA since={h1} prev=F1", "rerev-m", "low"), calls[1]
assert evs[1]["pass"] == "re-review" and "pass" not in evs[0]

shipped.clear()
state, calls, evs = run("t2", ["fix", "fix"])
assert state == "needs_human" and shipped == []

state, calls, evs = run("t3", ["approve"])
assert state == "pr_ready" and len(calls) == 1 and calls[0][0] == "FULL_DIFF"

# review after a fix round with no in-memory state (agy pickup / human resume):
# t2 ended on a `fix` review that recorded a head -> next review is a delta
shipped.clear()
tid = "t2"
seq = iter(["approve"])
calls2 = []


async def fake_review2(diff, model, emit, effort=None, worktree=None):
    calls2.append((diff, model, effort))
    return SimpleNamespace(outcome="reviewed", verdict=next(seq), cost=0.0,
                           quota_reset_at=None, findings=[])

lane.review = fake_review2  # type: ignore
prev_head = [e["data"] for e in store.events(tid) if e["kind"] == "review"][-1]["head"]
state = asyncio.run(loop._gate_review_ship(
    tid, Path("/wt"), "example", {}, "w", "rev-m", "low",
    lambda k, d: store.append_event(tid, k, d), review_effort="medium",
    rereview_model="rerev-m", rereview_effort="low"))
assert calls2[0][0].startswith(f"DELTA since={prev_head}") and calls2[0][1] == "rerev-m"
assert state == "pr_ready"
print("ok test_rereview_loop")

"""A fix round that changes nothing starts one fresh session; caps; worker-stalled.

    cd coxd && .venv/bin/python test_fresh_session.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-fresh-")

import gate  # noqa: E402
import lane  # noqa: E402
import loop  # noqa: E402
import notify  # noqa: E402
import quota  # noqa: E402
import store  # noqa: E402

notify.notify_async = lambda *a, **k: None
gate.diff = lambda wt: "diff"
gate._base_ref = lambda wt: "origin/main"
sys.modules["ship"] = SimpleNamespace(ship=lambda tid: ("local", None))

state = {"head": "aaa", "dirty": False}
calls: list[dict] = []
gates: list[int] = []
reviews: list[int] = []
script: list = []  # per worker call: "nochange" | "commit" | "quota" | "error"


def snap(wt):
    return state["head"], state["dirty"]


loop._snapshot = snap


async def fake_worker(wt, prompt, model, emit, resume=None, effort="medium", **k):
    calls.append({"prompt": prompt, "resume": resume})
    act = script.pop(0)
    if act == "commit":
        state["head"] += "x"
    return SimpleNamespace(cost=0.0, summary="s", session_id=f"s{len(calls)}",
                           is_error=act == "error",
                           quota_reset_at=123.0 if act == "quota" else None)


async def fake_review(*a, **k):
    reviews.append(1)
    return SimpleNamespace(outcome="reviewed", verdict="approve", cost=0.0,
                           quota_reset_at=None, findings=[])


lane.run_worker = fake_worker
lane.review = fake_review


def run(n, acts, gate_results=None):
    calls.clear()
    gates.clear()
    reviews.clear()
    script[:] = acts
    seq = list(gate_results or [False, True])

    def rg(wt, entry):
        gates.append(1)
        ok = seq.pop(0) if seq else True
        return {"passed": ok, "failing": "test", "reason": "boom", "steps": {}}

    gate.run_gate = rg
    tid = f"fresh-{n}"
    store.create_task(tid, "example-shop", "add a cart badge", "/wt", repo_path="/repo/x")
    store.set_session(tid, "s0")
    try:
        r = asyncio.run(loop._gate_review_ship(tid, Path("/wt"), "example-shop", {}, "w",
                                               "r", "low", lambda k, d: store.append_event(tid, k, d)))
    except quota.QuotaWait:
        r = "quota"
    return tid, r


def kinds(tid):
    return [e["kind"] for e in store.events(tid)]


# 1+2: stall -> fresh session (resume=None, feedback in prompt) -> commit -> continues
tid, r = run(1, ["nochange", "commit"])
assert calls[0]["resume"] == "s0" and calls[1]["resume"] is None
assert "boom" in calls[1]["prompt"] and "git diff origin/main...HEAD" in calls[1]["prompt"]
assert "Do not push" in calls[1]["prompt"]
assert kinds(tid).count("fresh-session") == 1
assert r == "pr_ready" and len(reviews) == 1
assert store.get_task(tid)["session_id"] == "s2"

# 3: fresh session also no change -> worker-stalled; no more gate/review
tid, r = run(2, ["nochange", "nochange"])
t = store.get_task(tid)
assert r == "needs_human" and t["reason"] == "worker-stalled", t
assert len(gates) == 1 and not reviews

# 4: usage limit is not a stall
tid, r = run(3, ["quota"])
assert r == "quota" and len(calls) == 1 and "fresh-session" not in kinds(tid)

# error result follows today's path (no fresh session)
tid, r = run(4, ["error"])
assert len(calls) == 1 and "fresh-session" not in kinds(tid)

# 5: per-task cap of two: two prior fresh sessions -> no third
tid = "fresh-cap"
store.create_task(tid, "example-shop", "b", "/wt", repo_path="/repo/x")
store.set_session(tid, "s0")
store.append_event(tid, "fresh-session", {})
store.append_event(tid, "fresh-session", {})
script[:] = ["nochange"]
calls.clear()
stalled = asyncio.run(loop._resume_fix(tid, Path("/wt"), "w", lambda k, d: None, "fix it"))
assert stalled and len(calls) == 1

# human note with expect_change=False: no-change is fine
script[:] = ["nochange"]
calls.clear()
assert not asyncio.run(loop._resume_fix(tid, Path("/wt"), "w", lambda k, d: None, "ok?",
                                        expect_change=False))
assert len(calls) == 1
print("ok test_fresh_session")

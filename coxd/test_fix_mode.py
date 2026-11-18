"""Per-repo `fix_mode`: resume (default) vs fresh fix sessions with a compact brief.

    cd coxd && .venv/bin/python test_fix_mode.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-fixmode-")

import gate  # noqa: E402
import lane  # noqa: E402
import loop  # noqa: E402
import notify  # noqa: E402
import registry  # noqa: E402
import sandbox  # noqa: E402
import store  # noqa: E402

notify.notify_async = lambda *a, **k: None
gate.diff = lambda wt, base=None: "DIFF-BODY +added line"
gate._base_ref = lambda wt: "origin/main"
sys.modules["ship"] = SimpleNamespace(ship=lambda tid: ("local", None))
loop.registry.load = lambda repo: None

_orig_run = sandbox.run_in_worktree


def fake_run(cmd, wt, *a, **k):
    if cmd[:2] == ["git", "diff"] and "--stat" in cmd:
        return SimpleNamespace(returncode=0, stdout=" cart.py | 3 +++\n", stderr="")
    return _orig_run(cmd, wt, *a, **k)


sandbox.run_in_worktree = fake_run
state = {"head": "aaa"}
loop._snapshot = lambda wt: (state["head"], False)
calls: list[dict] = []
review_script: list = []


async def fake_worker(wt, prompt, model, emit, resume=None, effort="medium", **k):
    calls.append({"prompt": prompt, "resume": resume})
    state["head"] += "x"
    return SimpleNamespace(cost=0.0, summary="s", session_id=f"n{len(calls)}",
                           is_error=False, quota_reset_at=None)


async def fake_review(*a, **k):
    v, f = review_script.pop(0) if review_script else ("approve", [])
    return SimpleNamespace(outcome="reviewed", verdict=v, cost=0.0,
                           quota_reset_at=None, findings=f)


lane.run_worker = fake_worker
lane.review = fake_review


def run(n, entry, gates, reviews=()):
    calls.clear()
    review_script[:] = list(reviews)
    seq = list(gates)

    def rg(wt, e):
        ok = seq.pop(0) if seq else True
        return {"passed": ok, "failing": "test", "reason": "GATE-BOOM", "steps": {}}

    gate.run_gate = rg
    tid = f"fm-{n}"
    store.create_task(tid, "example-shop", "add a cart badge", "/wt", repo_path="/repo/x")
    store.set_session(tid, "s0")
    r = asyncio.run(loop._gate_review_ship(
        tid, Path("/wt"), "example-shop", entry, "w", "r", "low",
        lambda k, d: store.append_event(tid, k, d)))
    return tid, r


def trig(tid):
    return [e["data"] for e in store.events(tid) if e["kind"] == "fix-trigger"]


# registry.fix_mode: exact "fresh" only
assert registry.fix_mode({}) == "resume" and registry.fix_mode(None) == "resume"
assert registry.fix_mode({"fix_mode": "resume"}) == "resume"
assert registry.fix_mode({"fix_mode": "fresh"}) == "fresh"
for bad in ("bogus", "FRESH", True, 1, None, ["fresh"]):
    assert registry.fix_mode({"fix_mode": bad}) == "resume", bad

# default + explicit resume + invalid: resume the original session, plain feedback
for i, entry in enumerate(({}, {"fix_mode": "resume"}, {"fix_mode": "bogus"})):
    tid, r = run(f"res{i}", entry, [False, True])
    assert r == "pr_ready" and len(calls) == 1, (r, calls)
    assert calls[0]["resume"] == "s0"
    assert calls[0]["prompt"].startswith("The gate failed at test:")
    assert "DIFF-BODY" not in calls[0]["prompt"]
    assert trig(tid)[0]["fix-mode"] == "resume"

# fresh, gate failure: new session, brief = intent + stat + diff + failure
tid, r = run("fresh-gate", {"fix_mode": "fresh"}, [False, True])
assert r == "pr_ready" and len(calls) == 1
c = calls[0]
assert c["resume"] is None
for needle in ("add a cart badge", "cart.py | 3 +++", "DIFF-BODY +added line",
               "GATE-BOOM", "Do not push"):
    assert needle in c["prompt"], needle
assert trig(tid)[0]["fix-mode"] == "fresh"
assert store.get_task(tid)["session_id"] == "n1"  # new session id stored

# fresh, review "fix" verdict, re-review approves
finding = [{"severity": "high", "summary": "FINDING-X"}]
tid, r = run("fresh-review", {"fix_mode": "fresh"}, [True, True],
             [("fix", finding), ("approve", [])])
assert r == "pr_ready" and len(calls) == 1
assert calls[0]["resume"] is None
assert "FINDING-X" in calls[0]["prompt"] and "DIFF-BODY" in calls[0]["prompt"]
assert trig(tid)[0]["source"] == "review" and trig(tid)[0]["fix-mode"] == "fresh"

# data guard survives into the fresh brief
loop.registry.load = lambda repo: {"data_globs": ["db/*.csv"], "fixture_dir": "fixtures"}
tid, r = run("fresh-guard", {"fix_mode": "fresh"}, [False, True])
assert "db/*.csv" in calls[0]["prompt"]
loop.registry.load = lambda repo: None

# oversized diff is capped
gate.diff = lambda wt, base=None: "Z" * (loop.MAX_BRIEF_DIFF_CHARS + 500)
tid, r = run("fresh-big", {"fix_mode": "fresh"}, [False, True])
assert "[truncated" in calls[0]["prompt"]
assert calls[0]["prompt"].count("Z") == loop.MAX_BRIEF_DIFF_CHARS
gate.diff = lambda wt, base=None: "DIFF-BODY +added line"

# human resume note keeps resuming, even in fresh mode
store.create_task("fm-note", "example-shop", "b", "/wt", repo_path="/repo/x")
store.set_session("fm-note", "s0")
loop.registry.load = lambda repo: {"fix_mode": "fresh"}
loop.registry.get_or_scout = lambda repo, wt: {"fix_mode": "fresh"}
gate.run_gate = lambda wt, entry: {"passed": True, "steps": {}}
calls.clear()
r = asyncio.run(loop.resume_task("fm-note", "use approach X", human_note=True))
assert calls[0]["resume"] == "s0" and calls[0]["prompt"] == "use approach X", calls
print("ok test_fix_mode")

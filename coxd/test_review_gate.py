"""A `fix` review verdict must be re-reviewed after the worker's fix; only a
fresh `approve` may ship (two PRs shipped with their reviewer's blocking
finding still open before this guard, 2026-10).

Standalone (coxd has no pytest harness). Throwaway home:
    cd coxd && COXD_HOME=$(mktemp -d) .venv/bin/python test_review_gate.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("COXD_HOME", tempfile.mkdtemp(prefix="coxd-review-"))

import gate  # noqa: E402
import lane  # noqa: E402
import loop  # noqa: E402
import notify  # noqa: E402
import store  # noqa: E402

shipped: list[str] = []
reviews: list[str] = []

gate.run_gate = lambda wt, entry: {"passed": True, "steps": {"test": "ok"}}  # type: ignore[assignment]
gate.diff = lambda wt: "diff"  # type: ignore[assignment]
notify.notify_async = lambda *a, **k: None  # type: ignore[assignment]


async def fake_worker(*a, **k):
    return SimpleNamespace(cost=0.0, summary="fixed", session_id="s1", quota_reset_at=None)

lane.run_worker = fake_worker  # type: ignore[assignment]

fake_ship = SimpleNamespace(ship=lambda tid: (shipped.append(tid) or ("local", None)))
sys.modules["ship"] = fake_ship  # loop imports ship lazily


def run(verdicts: list[str]) -> str:
    seq = iter(verdicts)

    async def fake_review(*a, **k):
        v = next(seq)
        reviews.append(v)
        return SimpleNamespace(outcome="reviewed", verdict=v, cost=0.0, quota_reset_at=None,
                               findings=[{"severity": "high", "file": "x", "message": "m"}])

    lane.review = fake_review  # type: ignore[assignment]
    tid = f"t-{len(reviews)}-{'-'.join(verdicts)}"
    store.create_task(tid, "example", "brief", "/wt", repo_path="/repo/example")
    store.set_session(tid, "s0")
    asyncio.run(loop._gate_review_ship(tid, Path("/wt"), "example", {}, "w", "r", "low",
                                       lambda *a, **k: None))
    return store.get_task(tid)["state"]


# fix -> worker fixes -> re-review still `fix`  => must NOT ship
shipped.clear()
assert run(["fix", "fix"]) == "needs_human"
assert shipped == [], "shipped despite an unresolved fix verdict"

# fix -> worker fixes -> re-review `approve`   => ships
assert run(["fix", "approve"]) == "pr_ready"
assert len(shipped) == 1

# approve on first pass => ships with a single review
n = len(reviews)
assert run(["approve"]) == "pr_ready"
assert len(reviews) == n + 1
print("ok test_review_gate")

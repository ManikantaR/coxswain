"""Round-robin across repos + per-repo daily cost cap. Run: python test_fair_share.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-test-")

import registry  # noqa: E402
import store  # noqa: E402
import supervisor  # noqa: E402

# 1. A has 3 queued, B has 1 -> A, B, A, A
for tid, repo in (("a1", "alpha"), ("a2", "alpha"), ("a3", "alpha"), ("b1", "bravo")):
    store.create_task(tid, repo, "b", "/wt")
order = [t["id"] for t in supervisor.fair_order(store.queued_tasks())]
assert order == ["a1", "b1", "a2", "a3"], order
# rotation: after starting alpha, bravo goes first
order = [t["id"] for t in supervisor.fair_order(store.queued_tasks(), "alpha")]
assert order[0] == "b1", order

# 2. over-cap repo is skipped, others still eligible
registry.save("alpha", {"daily_cost_cap": 5.0})
store.add_cost("a1", 5.0)
assert supervisor._over_cap("alpha")
assert not supervisor._over_cap("bravo")  # no registry entry -> no cap
registry.save("bravo", {"daily_cost_cap": 100})
assert not supervisor._over_cap("bravo")
eligible = [t["id"] for t in supervisor.fair_order(store.queued_tasks())
            if not supervisor._over_cap(t["repo"])]
assert eligible == ["b1"], eligible
# yesterday's spend doesn't count
assert store.cost_today("alpha", now=store.time.time() + 2 * 86400) == 0
print("ok")

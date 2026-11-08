"""One task per repo, several repos in parallel. Run: python test_repo_concurrency.py"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-test-")

import store  # noqa: E402
import supervisor  # noqa: E402

started = []


async def fake_run_one(tid, *a, **k):
    started.append(tid)
    await asyncio.sleep(30)

supervisor._run_one = fake_run_one


async def main():
    store.create_task("a1", "repo-a", "b", "/wt")
    store.create_task("a2", "repo-a", "b", "/wt")
    store.create_task("b1", "repo-b", "b", "/wt")
    r = asyncio.create_task(supervisor._runner(2, "w", "r", "m"))
    await asyncio.sleep(0.3)
    r.cancel()
    for t in supervisor._running.values():
        t.cancel()
    assert sorted(started) == ["a1", "b1"], started
    assert store.get_task("a2")["state"] == "queued"
    print("ok")

asyncio.run(main())

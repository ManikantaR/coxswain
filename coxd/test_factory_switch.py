"""Factory ON/OFF switch — the flag, the board API, and the runner honouring it.

Standalone (coxd has no pytest harness). Throwaway home:
    cd coxd && COXD_HOME=$(mktemp -d) ../.venv/bin/python test_factory_switch.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-factory-")

import board  # noqa: E402
import factory  # noqa: E402
import store  # noqa: E402
import supervisor  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


c = TestClient(board.app, base_url="http://localhost:8791")
_d = c.get("/api/factory").json()
check("default ON", _d["paused"] is False and "host" in _d and "role" in _d)
check("payload carries status", board._tasks_payload()["factory"]["paused"] is False)
r = c.post("/api/factory", json={"paused": True}).json()
check("POST paused → OFF", r["paused"] is True and factory.is_paused())
check("bad body → 400", c.post("/api/factory", json={"paused": "yes"}).status_code == 400)

# runner must not start a queued task while OFF, and must start it once ON
started: list[str] = []


async def fake_run_one(tid, *_a, **_k):
    started.append(tid)


supervisor._run_one = fake_run_one
store.create_task("q1", "repo", "brief", "/wt")


async def tick(seconds: float) -> None:
    t = asyncio.create_task(supervisor._runner(1, "w", "r", "medium"))
    await asyncio.sleep(seconds)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass


asyncio.run(tick(1.5))
check("OFF: queued task not started", started == [])
c.post("/api/factory", json={"paused": False})
asyncio.run(tick(1.5))
check("ON: queued task started", started == ["q1"])

print("OK" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)

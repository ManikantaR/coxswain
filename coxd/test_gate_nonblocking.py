"""While a slow gate runs, the event loop must stay responsive (/api/tasks < 0.5s).

Standalone: cd coxd && COXD_HOME=$(mktemp -d) python test_gate_nonblocking.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-nb-")
os.environ.pop("COXD_OWNER_USER", None)
os.environ.pop("COXD_PROXY_SECRET", None)

import board  # noqa: E402
import gate  # noqa: E402
import loop  # noqa: E402
import store  # noqa: E402


def slow_gate(wt, entry):
    time.sleep(2)
    return {"passed": False, "failing": "test", "reason": "x", "steps": {}}


gate.run_gate = slow_gate  # type: ignore[assignment]
store.create_task("nb1", "example", "brief", "/wt", repo_path="/repo/example")


async def main() -> float:
    gate_task = asyncio.create_task(loop._gate_review_ship(
        "nb1", Path("/wt"), "example", {}, "w", "r", "low", lambda *a, **k: None))
    await asyncio.sleep(0.2)  # gate is now running in its worker thread
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": "/api/tasks", "raw_path": b"/api/tasks",
             "query_string": b"", "root_path": "", "client": ("127.0.0.1", 1),
             "server": ("localhost", 80), "headers": [(b"host", b"localhost")]}
    msgs: list = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        msgs.append(m)

    t0 = time.monotonic()
    await asyncio.wait_for(board.app(scope, receive, send), 5)
    dt = time.monotonic() - t0

    class r:  # noqa: N801
        status_code = msgs[0]["status"]

    assert r.status_code == 200, r.status_code
    assert not gate_task.done(), "gate finished before the request; test proves nothing"
    gate_task.cancel()
    return dt


dt = asyncio.run(main())
assert dt < 0.5, f"/api/tasks took {dt:.2f}s while gate ran"
print(f"ok test_gate_nonblocking ({dt:.3f}s)")

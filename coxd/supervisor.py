"""coxd serve — the supervisor (DESIGN-V35).

ONE long-running process that (a) serves the board and (b) runs queued tasks as
asyncio tasks, up to a concurrency cap, all against the single store. This IS the
orchestrator daemon — no watcher, no pid files, no polling of log files. On the
NAS it runs 24/7 so AFK is real; if it restarts, the store is the truth and
in-flight sessions resume.
"""

from __future__ import annotations

import asyncio

import board
import loop
import store
import uvicorn


async def _run_one(task_id: str, worker_model: str, review_model: str) -> None:
    try:
        await loop.run_task(task_id, worker_model, review_model)
    except Exception as e:  # a crashing task must not take down the supervisor
        store.set_state(task_id, "needs_human", "coxd-error")
        store.append_event(task_id, "error", {"error": str(e)})


async def _runner(concurrency: int, worker_model: str, review_model: str) -> None:
    running: dict[str, asyncio.Task] = {}
    while True:
        for tid in [t for t, task in running.items() if task.done()]:
            del running[tid]
        if len(running) < concurrency:
            for t in store.queued_tasks():
                if len(running) >= concurrency:
                    break
                if t["id"] in running:
                    continue
                store.set_state(t["id"], "working")  # claim before the next scan
                running[t["id"]] = asyncio.create_task(
                    _run_one(t["id"], worker_model, review_model))
        await asyncio.sleep(1)


async def serve(host: str = "0.0.0.0", port: int = 8791, concurrency: int = 2,  # noqa: S104
                worker_model: str = "claude-haiku-4-5",
                review_model: str = "claude-sonnet-4-6") -> None:
    config = uvicorn.Config(board.app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    print(f"coxd → http://{host}:{port}/  (concurrency {concurrency})")
    await asyncio.gather(server.serve(), _runner(concurrency, worker_model, review_model))

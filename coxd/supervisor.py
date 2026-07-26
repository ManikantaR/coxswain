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
import landed
import loop
import store
import uvicorn

_CLEANUP_INTERVAL_S = 60


def _reclaim_orphans() -> None:
    """A prior supervisor process (hard-killed, crashed, or replaced by a bare
    `python -m board` that never runs `_runner`) can leave a task sitting in an
    active state (working/gating/fixing/reviewing/shipping/ci-watching) with no
    asyncio.Task actually driving it — indistinguishable, from the board alone,
    from a task that's merely quiet for a moment. Since _runner only ever
    creates tasks for things it pulls from queued_tasks() itself, anything
    already in an active state at THIS process's startup could not have been
    started by it — so it's orphaned by definition. Flag it instead of leaving
    it to sit forever looking deceptively normal (see 2026-07-25 coxd stall)."""
    for t in store.list_tasks():
        if t["state"] in board._ACTIVE:
            store.set_state(t["id"], "needs_human", "orphaned-on-restart")
            store.append_event(t["id"], "error", {
                "error": f"orphaned: no worker was running (was '{t['state']}') "
                         "when this supervisor started — a prior process died "
                         "or was replaced mid-task",
            })


async def _run_one(task_id: str, worker_model: str, review_model: str,
                   effort: str) -> None:
    try:
        await loop.run_task(task_id, worker_model, review_model, effort=effort)
    except Exception as e:  # a crashing task must not take down the supervisor
        store.set_state(task_id, "needs_human", "coxd-error")
        store.append_event(task_id, "error", {"error": str(e)})


async def _runner(concurrency: int, worker_model: str, review_model: str,
                  effort: str) -> None:
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
                    _run_one(t["id"], worker_model, review_model, effort))
        await asyncio.sleep(1)


async def _cleanup_loop() -> None:
    """Detect merged PRs and do the bookkeeping (worktree/branch/issue) that used
    to be a manual step after every single merge — explicitly confirmed as
    automatic/unattended (2026-07-21). A crashing pass must not take down the
    supervisor — same posture as _run_one."""
    while True:
        try:
            await asyncio.to_thread(landed.check_and_cleanup_merged)
        except Exception as e:  # noqa: BLE001 - log and keep polling, never die
            print(f"cleanup pass failed: {e}")
        await asyncio.sleep(_CLEANUP_INTERVAL_S)


async def serve(host: str = "0.0.0.0", port: int = 8791, concurrency: int = 2,  # noqa: S104
                worker_model: str = "claude-sonnet-5",
                review_model: str = "claude-opus-4-8",
                effort: str = "medium") -> None:
    _reclaim_orphans()
    config = uvicorn.Config(board.app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    print(f"coxd → http://{host}:{port}/  (concurrency {concurrency}, effort {effort})")
    await asyncio.gather(server.serve(),
                         _runner(concurrency, worker_model, review_model, effort),
                         _cleanup_loop())

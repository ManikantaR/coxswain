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

# Populated by `_runner`, read by `cancel()` — lets the board stop an in-flight
# task (kill its asyncio.Task, which propagates a CancelledError into whatever
# the Agent SDK call is awaiting) instead of the only prior option, which was
# racing it to `gh pr merge` before a mistaken /retry could finish redoing the
# work from scratch (see 2026-07-27 duplicate-PR-178/180 incident).
_running: dict[str, asyncio.Task] = {}

# reason markers set on orphans at restart so `_runner` knows HOW to recover each
# (see `_reclaim_orphans`): resume the preserved SDK session, or start fresh.
_AUTO_RESUME = "auto-resume-on-restart"
_AUTO_RETRY = "auto-retry-on-restart"


def cancel(task_id: str) -> bool:
    task = _running.get(task_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def _reclaim_orphans() -> None:
    """A prior supervisor process (hard-killed, crashed, or replaced mid-task) can
    leave a task sitting in an active state (working/gating/fixing/reviewing/
    shipping/ci-watching) with no asyncio.Task driving it. Since _runner only ever
    creates tasks for things it pulls from queued_tasks() itself, anything already
    in an active state at THIS process's startup could not have been started by it
    — so it's orphaned by definition (see 2026-07-25 coxd stall).

    Recover it automatically rather than parking it at needs_human (the crash-resume
    bar: a restart must resume and ship, not demand a human). The recovery is chosen
    by how far the task got — pure state transitions here; `_runner` does the actual
    resume/retry when it next scans:
      - a PR is already open  → needs_human. Auto-retry would open a DUPLICATE PR
        (2026-07-27 dup-PR-178/180 incident) and re-shipping via resume could too;
        one human glance is the cheapest safe option and this is rare.
      - a session_id exists   → auto-RESUME: continue the preserved SDK session where
        it left off (no duplicated work, keeps any commits).
      - no session yet        → auto-RETRY fresh: it crashed during provisioning or the
        first worker call, before anything was committed — a clean re-run is safe."""
    for t in store.list_tasks():
        if t["state"] not in board._ACTIVE:
            continue
        tid = t["id"]
        if t.get("pr_url"):
            store.set_state(tid, "needs_human", "orphaned-on-restart")
            store.append_event(tid, "error", {
                "error": f"orphaned after restart with an open PR ({t['pr_url']}) — "
                         "resume or close/merge the PR manually to avoid a duplicate",
            })
        elif t.get("session_id"):
            store.set_state(tid, "queued", _AUTO_RESUME)
            store.append_event(tid, "info", {
                "note": f"auto-resume queued after coxd restart (was '{t['state']}', "
                        "session preserved)",
            })
        else:
            store.set_state(tid, "queued", _AUTO_RETRY)
            store.append_event(tid, "info", {
                "note": f"auto-retry queued after coxd restart (was '{t['state']}', "
                        "no session/commits yet)",
            })


async def _run_one(task_id: str, worker_model: str, review_model: str,
                   effort: str) -> None:
    try:
        await loop.run_task(task_id, worker_model, review_model, effort=effort)
    except asyncio.CancelledError:
        # A human hit Stop (see `cancel()` above) — distinct from coxd-error so
        # the board doesn't read this as a crash. Not retryable via /retry from
        # here since real work may already be committed; /resume is the un-stick.
        store.set_state(task_id, "needs_human", "stopped-by-user")
        store.append_event(task_id, "error", {"error": "stopped by user"})
    except Exception as e:  # a crashing task must not take down the supervisor
        store.set_state(task_id, "needs_human", "coxd-error")
        store.append_event(task_id, "error", {"error": str(e)})


async def _run_one_resume(task_id: str, worker_model: str, review_model: str,
                          effort: str) -> None:
    """Auto-recovery for an orphan that had a live SDK session (see
    `_reclaim_orphans`): resume that session where it left off and re-run the
    gate->review->ship tail, instead of restarting from scratch. Same crash
    posture as `_run_one` — a failure lands at needs_human, never kills serve."""
    try:
        await loop.resume_task(
            task_id,
            "coxd restarted while this task was mid-flight — continue from where you "
            "left off. Re-check the working tree first; some work may already be committed.",
            worker_model, review_model, effort=effort)
    except asyncio.CancelledError:
        store.set_state(task_id, "needs_human", "stopped-by-user")
        store.append_event(task_id, "error", {"error": "stopped by user"})
    except Exception as e:  # a crashing task must not take down the supervisor
        store.set_state(task_id, "needs_human", "coxd-error")
        store.append_event(task_id, "error", {"error": str(e)})


async def _runner(concurrency: int, worker_model: str, review_model: str,
                  effort: str) -> None:
    while True:
        for tid in [t for t, task in _running.items() if task.done()]:
            del _running[tid]
        if len(_running) < concurrency:
            for t in store.queued_tasks():
                if len(_running) >= concurrency:
                    break
                if t["id"] in _running:
                    continue
                # An orphan reclaimed for auto-resume (session preserved) continues
                # its SDK session; everything else — fresh dispatches AND board /retry,
                # which clears the reason — starts from the brief.
                if t.get("reason") == _AUTO_RESUME and t.get("session_id"):
                    store.set_state(t["id"], "fixing")  # claim before the next scan
                    _running[t["id"]] = asyncio.create_task(
                        _run_one_resume(t["id"], worker_model, review_model, effort))
                else:
                    store.set_state(t["id"], "working")  # claim before the next scan
                    _running[t["id"]] = asyncio.create_task(
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

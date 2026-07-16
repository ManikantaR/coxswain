"""coxd CLI — dispatch | serve | tail | list (DESIGN-V35).

Run from coxd/:  .venv/bin/python cli.py serve
                 .venv/bin/python cli.py dispatch <repo> "<brief>"
COXD_HOME selects the store/registry/worktrees home.
"""

from __future__ import annotations

import argparse
import asyncio
import time

import dispatch as dispatch_mod
import store
import supervisor


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="coxd")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dispatch", help="queue a task (the serve runner picks it up)")
    d.add_argument("repo")
    d.add_argument("brief")

    s = sub.add_parser("serve", help="run the supervisor: board + task runner")
    s.add_argument("--port", type=int, default=8791)
    s.add_argument("--concurrency", type=int, default=2)

    t = sub.add_parser("tail", help="follow a task's event log to a terminal state")
    t.add_argument("task_id")

    sub.add_parser("list", help="list tasks")

    a = p.parse_args(argv)

    if a.cmd == "dispatch":
        print("queued:", dispatch_mod.dispatch(a.repo, a.brief))
        return 0
    if a.cmd == "serve":
        asyncio.run(supervisor.serve(port=a.port, concurrency=a.concurrency))
        return 0
    if a.cmd == "list":
        for row in store.list_tasks():
            print(f"{row['state']:<12} {row['id']:<40} ${row['cost'] or 0:.3f}")
        return 0
    if a.cmd == "tail":
        seen = 0
        while True:
            for e in store.events(a.task_id, seen):
                print(f"  [{e['kind']:<10}] {str(e['data'])[:100]}")
                seen = e["seq"]
            row = store.get_task(a.task_id)
            if row and row["state"] in ("pr_ready", "landed", "needs_human", "failed"):
                print(f"=> {row['state']}  {row['reason'] or ''}  ${row['cost'] or 0:.3f}")
                return 0
            time.sleep(1)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

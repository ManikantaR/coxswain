"""cox — control-plane CLI (T-01, wired incrementally through M0).

The orchestrator (an agent session) acts ONLY through these subcommands, which
enforce the guardrails (DESIGN §4). Commands print terse, machine-friendly
lines so the orchestrator spends few tokens reading them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import home, store, wakequeue, watch
from .model import DispatchPath


def _cmd_status(args: argparse.Namespace) -> int:
    ids = store.list_task_ids()

    if args.json:
        rows = []
        for tid in ids:
            m = store.load_meta(tid)
            cost_entries = store.read_cost(tid)
            _, _, cost_usd = store.cost_total(tid)
            rows.append({
                "id": m.id,
                "repo": m.repo,
                "state": m.state.value,
                "reason": m.reason.value if m.reason else None,
                "path": m.path.value,
                "lane": m.lane,
                "model": m.model,
                "cost_usd": cost_usd if cost_entries else None,
                "dispatched_at": m.dispatched_at,
            })
        print(json.dumps(rows))
        return 0

    age = watch.heartbeat_age()
    in_flight = any(store.load_meta(t).state.value in {"working", "gating", "fixing"} for t in ids)
    if in_flight and (age is None or age > 60):
        print("!! WATCHER STALE — tasks in flight but no heartbeat. Run `cox watch`.")
    if home.is_paused():
        print("PAUSED (state/PAUSED present) — dispatch/ship halted")
    if not ids:
        print("no tasks")
    for tid in ids:
        m = store.load_meta(tid)
        tin, tout, cost = store.cost_total(tid)
        cost_str = f"${cost:.2f}" if cost is not None else "?"
        reason = f" [{m.reason.value}]" if m.reason else ""
        print(
            f"{tid}  {m.state.value}{reason}  {m.lane}/{m.model}  "
            f"{tin}+{tout}tok {cost_str}  pr={m.pr_url or '-'}"
        )
    if args.wakes:
        pending = wakequeue.undelivered()
        print(f"-- {len(pending)} undelivered wake(s)")
        for w in pending:
            print(f"WAKE {w.task_id} {w.verb}: {w.detail}")
    return 0


def _cmd_await_wake(args: argparse.Namespace) -> int:
    import time

    deadline = time.time() + args.timeout if args.timeout else None
    while True:
        pending = wakequeue.undelivered()
        if pending:
            for w in pending:
                print(f"WAKE {w.task_id} {w.verb}: {w.detail}")
            wakequeue.mark_delivered({w.key for w in pending})
            return 0
        if deadline and time.time() >= deadline:
            print("-- no wakes (timeout)")
            return 0
        time.sleep(2)


def _cmd_watch(args: argparse.Namespace) -> int:
    if args.once:
        n = watch.scan_once()
        print(f"scan enqueued {n} wake(s)")
        return 0
    watch.run()  # pragma: no cover
    return 0


def _cmd_peek(args: argparse.Namespace) -> int:
    log = store.task_data_dir(args.task_id) / "worker.log"
    if not log.exists():
        print(f"no worker log for {args.task_id}")
        return 1
    if args.raw:
        # escape hatch: last 40 raw stream-json lines (capped)
        for line in log.read_text(encoding="utf-8").splitlines()[-40:]:
            print(line)
        return 0
    # default: compact narrated activity feed — cheap for the orchestrator to
    # read, and the same renderer the glance dashboard reuses (DESIGN: never
    # stream a whole log). Header line first for one-glance context.
    from . import render

    m = store.load_meta(args.task_id)
    _, _, cost = store.cost_total(args.task_id)
    cost_str = f"${cost:.2f}" if cost is not None else "$?"
    print(f"[{args.task_id}] {m.state.value}  {m.lane}/{m.model}  {cost_str}")
    for line in render.summarize_stream(log, n=args.lines):
        print(line)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from . import server

    server.serve(host=args.host, port=args.port, token=args.token)
    return 0


def _cmd_pause(args: argparse.Namespace) -> int:
    home.set_paused(True)
    print("PAUSED")
    return 0


def _cmd_resume_ops(args: argparse.Namespace) -> int:
    home.set_paused(False)
    print("resumed")
    return 0


def _cmd_cost(args: argparse.Namespace) -> int:
    tin, tout, cost = store.cost_total(args.task_id)
    print(f"{args.task_id}: {tin} in / {tout} out / {f'${cost:.4f}' if cost is not None else '?'}")
    return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    from . import dispatch as disp

    meta = disp.dispatch(
        repo_path=Path(args.repo),
        title=args.title,
        body=args.body or args.title,
        path=DispatchPath(args.path),
        lane=args.lane,
    )
    print(f"dispatched {meta.id} ({meta.path.value}) on {meta.lane}/{meta.model}")
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    from . import gate

    report = gate.run_gate(args.task_id)
    verdict = "PASS" if report.passed else f"RED@{report.failing_step or ''}"
    print(f"gate {args.task_id}: {verdict}")
    return 0 if report.passed else 1


def _cmd_fix(args: argparse.Namespace) -> int:
    from . import fix as fixmod

    try:
        meta = fixmod.fix(args.task_id, notes=args.notes)
    except fixmod.FixCapReached as e:
        print(str(e))
        return 1
    print(f"fix round {meta.fix_rounds} started for {args.task_id}")
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    from . import review

    outcome = review.review(args.task_id)
    n = len(outcome.findings)
    print(f"review {args.task_id}: route={outcome.route} verdict={outcome.verdict} findings={n}")
    return 0 if outcome.route == "approve" else 1


def _cmd_ship(args: argparse.Namespace) -> int:
    from . import ship

    meta = ship.ship(args.task_id, Path(args.repo), args.title)
    print(f"ship {args.task_id}: {meta.state.value} pr={meta.pr_url or '-'}")
    return 0 if meta.pr_url else 1


def _cmd_merge(args: argparse.Namespace) -> int:
    from . import ship

    meta = ship.merge(args.task_id, Path(args.repo))
    print(f"merged {args.task_id}: {meta.state.value}")
    return 0


def _cmd_teardown(args: argparse.Namespace) -> int:
    from . import worktree

    meta = store.load_meta(args.task_id)
    from .repoconfig import load_repo_config

    cfg = load_repo_config(Path(meta.worktree))
    wt = worktree.Worktree(path=Path(meta.worktree), branch=meta.branch)
    try:
        worktree.remove(Path(args.repo), wt, cfg.target_branch, force=args.force)
    except worktree.UnlandedWorkError as e:
        print(str(e))
        lost = worktree.unlanded_commits(Path(args.repo), wt, cfg.target_branch)
        for c in lost:
            print(f"  would lose: {c}")
        return 1
    print(f"tore down {args.task_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cox", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", help="fleet overview + cost")
    s.add_argument("--wakes", action="store_true", help="also list undelivered wakes")
    s.add_argument("--json", action="store_true", help="emit JSON array of task objects")
    s.set_defaults(func=_cmd_status)

    s = sub.add_parser("await-wake", help="block until a wake is available (orchestrator's ear)")
    s.add_argument("--timeout", type=float, default=0)
    s.set_defaults(func=_cmd_await_wake)

    s = sub.add_parser("watch", help="run the watcher loop")
    s.add_argument("--once", action="store_true", help="single scan then exit (tests/cron)")
    s.set_defaults(func=_cmd_watch)

    s = sub.add_parser("peek", help="compact narrated activity feed for a task")
    s.add_argument("task_id")
    s.add_argument("--lines", type=int, default=15, help="feed events to show (default 15)")
    s.add_argument("--raw", action="store_true", help="dump last 40 raw stream-json lines instead")
    s.set_defaults(func=_cmd_peek)

    s = sub.add_parser("dispatch", help="spawn a worker task")
    s.add_argument("repo")
    s.add_argument("title")
    s.add_argument("--body", default=None)
    s.add_argument("--path", choices=[p.value for p in DispatchPath], default="full")
    s.add_argument("--lane", default="claude")
    s.set_defaults(func=_cmd_dispatch)

    s = sub.add_parser("gate", help="run deterministic gate steps 1-4")
    s.add_argument("task_id")
    s.set_defaults(func=_cmd_gate)

    s = sub.add_parser("fix", help="resumed fix round")
    s.add_argument("task_id")
    s.add_argument("--notes", default=None)
    s.set_defaults(func=_cmd_fix)

    s = sub.add_parser("review", help="one read-only review pass + verdict routing")
    s.add_argument("task_id")
    s.set_defaults(func=_cmd_review)

    s = sub.add_parser("ship", help="push + open PR")
    s.add_argument("task_id")
    s.add_argument("repo")
    s.add_argument("title")
    s.set_defaults(func=_cmd_ship)

    s = sub.add_parser("merge", help="merge the PR (captain's word)")
    s.add_argument("task_id")
    s.add_argument("repo")
    s.set_defaults(func=_cmd_merge)

    s = sub.add_parser("teardown", help="fail-closed worktree removal")
    s.add_argument("task_id")
    s.add_argument("repo")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=_cmd_teardown)

    s = sub.add_parser("pause", help="halt dispatch/ship")
    s.set_defaults(func=_cmd_pause)

    s = sub.add_parser("resume-ops", help="clear the pause flag")
    s.set_defaults(func=_cmd_resume_ops)

    s = sub.add_parser("cost", help="per-task token/cost total")
    s.add_argument("task_id")
    s.set_defaults(func=_cmd_cost)

    s = sub.add_parser("serve", help="glance dashboard (web, desktop + phone on LAN)")
    s.add_argument("--host", default="127.0.0.1", help="127.0.0.1 (local) or 0.0.0.0 (LAN/phone)")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--token", default=None, help="shared access token (default: random)")
    s.set_defaults(func=_cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

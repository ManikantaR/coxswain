"""Stall ping (once), quiet-hours silence, and the once-a-day morning summary.

Standalone (coxd has no pytest harness). Throwaway home:
    cd coxd && COXD_HOME=$(mktemp -d) ../.venv/bin/python test_watchdog.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-wd-")

import notify  # noqa: E402
import store  # noqa: E402
import watchdog  # noqa: E402

fails = 0
sent: list[tuple] = []
notify.notify_async = lambda title, msg, priority="default": sent.append((title, msg))


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


ET = ZoneInfo("America/New_York")
# quiet hours (default 22:00-07:15, wraps midnight)
check("23:30 is quiet", notify.in_quiet_hours(datetime(2026, 11, 2, 23, 30, tzinfo=ET)))
check("03:00 is quiet", notify.in_quiet_hours(datetime(2026, 11, 2, 3, 0, tzinfo=ET)))
check("07:15 is not quiet", not notify.in_quiet_hours(datetime(2026, 11, 2, 7, 15, tzinfo=ET)))
check("14:00 is not quiet", not notify.in_quiet_hours(datetime(2026, 11, 2, 14, 0, tzinfo=ET)))

# stall: quiet 50 min → one ping; second pass → no repeat
store.create_task("w1", "repo", "brief", "/wt")
store.set_state("w1", "working")
store.append_event("w1", "say", {"text": "thinking"})
later = time.time() + 50 * 60
check("stalled task pinged", watchdog.check_stalls(later) == ["w1"])
check("not pinged twice", watchdog.check_stalls(later + 600) == [])
store.append_event("w1", "say", {"text": "moving again"})
check("fresh activity → no ping", watchdog.check_stalls(time.time() + 60) == [])

# summary content
store.create_task("r1", "repo", "brief", "/wt")
store.set_pr_url("r1", "https://github.com/o/r/pull/1")
store.set_state("r1", "pr_ready")
store.create_task("h1", "repo", "brief", "/wt")
store.set_state("h1", "needs_human", "gate-red")
txt = watchdog.summary_text()
check("summary lists ready PR", "Ready to merge (1)" in txt and "pull/1" in txt)
check("summary lists needs-you + reason", "Needs you (1)" in txt and "gate-red" in txt)
check("summary lists running", "Running: w1" in txt)

# morning summary: not before 07:20, once per day
sent.clear()
send = watchdog.maybe_send_morning_summary
check("07:10 → not yet", not send(datetime(2026, 11, 2, 7, 10, tzinfo=ET)))
check("07:21 → sent", send(datetime(2026, 11, 2, 7, 21, tzinfo=ET)))
check("12:00 same day → not again", not send(datetime(2026, 11, 2, 12, 0, tzinfo=ET)))
check("next day → sent", send(datetime(2026, 11, 3, 7, 25, tzinfo=ET)))
check("two summaries total", len([s for s in sent if "morning" in s[0]]) == 2)

print("OK" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)

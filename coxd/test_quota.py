"""Usage limit → park at waiting_quota → resume the same session when the window resets.

Standalone (coxd has no pytest harness). Throwaway home:
    cd coxd && COXD_HOME=$(mktemp -d) ../.venv/bin/python test_quota.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-quota-")

import quota  # noqa: E402
import store  # noqa: E402
import supervisor  # noqa: E402

fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


now = time.time()
# detection
check("rejected → resets_at", quota.rejected_reset_at(NS(status="rejected", resets_at=1793168400))
      == 1793168400.0)
check("allowed → None", quota.rejected_reset_at(NS(status="allowed", resets_at=1)) is None)
check("rejected without time → fallback",
      quota.rejected_reset_at(NS(status="rejected", resets_at=None)) > now + 1000)
check("429 result → fallback", quota.reset_from_result(NS(api_error_status=429, result="")) > now)
check("CLI text with epoch",
      quota.reset_from_result(NS(api_error_status=None,
                                 result="Claude AI usage limit reached|1793168400"))
      == 1793168400.0)
check("normal result → None", quota.reset_from_result(NS(api_error_status=None, result="ok"))
      is None)

# park / block / release
store.create_task("s1", "repo", "brief", "/wt")
store.set_session("s1", "sess-1")
store.create_task("n1", "repo", "brief", "/wt")
r = quota.park("s1", now + 600, "implement")
check("park → waiting_quota", store.get_task("s1")["state"] == "waiting_quota")
check("resume_at = reset + buffer", now + 600 + quota.RESUME_BUFFER_S <= r <= now + 600 + 151)
check("factory blocked while waiting", quota.blocked_until() == r)
check("not released early", quota.release_due("retry") == [])
quota.park("n1", now - 1000, "implement")  # already past its time
released = quota.release_due("retry")
check("past-due released", released == ["n1"])
check("no session → fresh retry reason", store.get_task("n1")["reason"] == "retry")
store.set_resume_at("s1", now - 1)
quota.release_due("retry")
t = store.get_task("s1")
check("with session → queued for quota resume",
      t["state"] == "queued" and t["reason"] == quota.QUOTA_RESUME and t["resume_at"] is None)
store.set_state("n1", "landed")

# runner: a task that hits the limit is parked, nothing else starts, then it resumes
store.set_state("s1", "landed")
store.create_task("q1", "repo", "brief", "/wt")
store.create_task("q2", "repo", "brief", "/wt")
calls: list[tuple] = []


async def fake_run_task(tid, *a, **k):
    calls.append(("run", tid))
    store.set_session(tid, "sess-" + tid)
    if tid == "q1":
        raise quota.QuotaWait(time.time() + 2, "implement")
    store.set_state(tid, "pr_ready")


async def fake_resume(tid, wm, rm, effort, after_quota=False):
    calls.append(("resume", tid, after_quota))
    store.set_state(tid, "pr_ready")


supervisor.loop.run_task = fake_run_task
supervisor._run_one_resume = fake_resume
quota.RESUME_BUFFER_S = 0
quota.random.uniform = lambda a, b: 0


async def run_for(seconds: float) -> None:
    t = asyncio.create_task(supervisor._runner(1, "w", "r", "medium"))
    await asyncio.sleep(seconds)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass


asyncio.run(run_for(1.5))
check("q1 parked", store.get_task("q1")["state"] == "waiting_quota")
check("q2 NOT started while blocked", ("run", "q2") not in calls)
asyncio.run(run_for(4))
check("q1 resumed its session after reset", ("resume", "q1", True) in calls)
check("q2 ran after the block lifted", ("run", "q2") in calls)

print("OK" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)

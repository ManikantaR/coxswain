"""Lead review fixes go back to the same session and onto the same open PR.

Standalone (coxd has no pytest harness). Throwaway home:
    cd coxd && COXD_HOME=$(mktemp -d) ../.venv/bin/python test_resume_pr_ready.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("COXD_HOME", tempfile.mkdtemp(prefix="coxd-resume-"))

import board  # noqa: E402
import ship  # noqa: E402
import store  # noqa: E402
import supervisor  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


c = TestClient(board.app, base_url="http://localhost:8791")
store.create_task("p1", "repo", "brief", "/wt", repo_path="/repo/x")
store.set_session("p1", "sess-1")
store.set_pr_url("p1", "https://github.com/o/r/pull/9")
store.set_state("p1", "pr_ready")
check("payload marks it resumable",
      next(r for r in board._tasks_payload()["tasks"] if r["id"] == "p1")["resumable"])

r = c.post("/api/task/p1/resume", json={"note": "label the covered month"})
t = store.get_task("p1")
check("resume from pr_ready accepted + queued",
      r.status_code == 200 and t["state"] == "queued" and t["reason"] == board.HUMAN_RESUME)
check("note recorded", supervisor._latest_resume_note("p1") == "label the covered month")
check("landed task refused", (store.create_task("l1", "repo", "b", "/wt"),
      store.set_state("l1", "landed"),
      c.post("/api/task/l1/resume", json={"note": "x"}).status_code)[-1] == 400)

seen: list = []


async def fake_resume(tid, wm, rm, effort, note=None, **k):
    seen.append((tid, note))
    store.set_state(tid, "pr_ready")


supervisor._run_one_resume = fake_resume


async def run_for(seconds: float) -> None:
    t = asyncio.create_task(supervisor._runner(1, "w", "r", "medium"))
    await asyncio.sleep(seconds)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass


asyncio.run(run_for(1.5))
check("supervisor ran it with the human note", seen == [("p1", "label the covered month")])

# ship: an existing PR is reused, never a second `gh pr create`
calls: list = []


def fake_run(cmd, **k):
    calls.append(cmd)
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


ship.subprocess.run = fake_run
ship._has_gh_remote = lambda p: True
out = ship.ship("p1")
check("ship reuses the open PR", out == ("pr", "https://github.com/o/r/pull/9"))
check("pushed, no pr create", any(c[:2] == ["git", "push"] for c in calls)
      and not any(c[:3] == ["gh", "pr", "create"] for c in calls))

print("OK" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)

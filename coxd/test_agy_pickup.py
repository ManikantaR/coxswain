"""agy handoff lane 3/4: pick up the pushed coxd-agy/<id> branch, gate, review, choose.

    cd coxd && python test_agy_pickup.py
(real local git + bare origin; gate/review/worker/gh mocked; invented data only.)
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-pick-")
os.environ["COXD_WORKTREES"] = tempfile.mkdtemp(prefix="coxd-pick-wt-")
tempfile.tempdir = tempfile.mkdtemp(prefix="coxd-pick-tmp-")
os.environ.pop("COXD_WORKER_USER", None)  # sandbox.wrap is a no-op off the NAS

import agy_handoff  # noqa: E402
import board  # noqa: E402
import ci_triage  # noqa: E402
import gate  # noqa: E402
import lane  # noqa: E402
import loop  # noqa: E402
import notify  # noqa: E402
import pii  # noqa: E402
import quota  # noqa: E402
import registry  # noqa: E402
import ship  # noqa: E402
import store  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

fails = 0
open(pii.denylist_path(), "w").write("Zebulon Quux\n")


def check(name, cond):
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True,
                          env=ENV).stdout


root = Path(tempfile.mkdtemp(prefix="coxd-pick-git-"))
origin, repo, human = root / "origin.git", root / "widgets", root / "human"
git(root, "init", "--bare", "-b", "main", str(origin))
git(root, "init", "-b", "main", str(repo))
(repo / "a.py").write_text("x = 1\n")
git(repo, "add", ".")
git(repo, "commit", "-m", "init")
git(repo, "remote", "add", "origin", str(origin))
git(repo, "push", "origin", "main")
git(root, "clone", str(origin), str(human))

pings: list = []
notify.notify_async = lambda title, msg, priority="default": pings.append((title, msg))
registry.save("widgets", {"agy_send_enabled": True})  # today's paths; default is off
ci_triage.repo_slug = lambda p: None  # no CI watch
n = [0]


def new_agy_task() -> str:
    n[0] += 1
    tid = f"widgets-agy{n[0]}"
    wt = Path(os.environ["COXD_WORKTREES"]) / tid
    git(repo, "worktree", "add", "-b", f"coxd/{tid}", str(wt), "main")
    store.create_task(tid, "widgets", "Add thing", str(wt), repo_path=str(repo),
                      intent="Add thing", lane="agy-handoff")

    async def fake_plan(brief, model, emit, effort="medium", *, worktree):
        return lane.PlanOutcome("planned", "## Files\n- a.py\n", 0.01)
    lane.plan = fake_plan
    asyncio.run(loop.run_task(tid))
    return tid


def human_push(tid, text):
    git(human, "fetch", "origin")
    git(human, "checkout", "-B", "w", f"origin/coxd-agy/{tid}")
    (human / "b.py").write_text(text)
    git(human, "add", ".")
    git(human, "commit", "-m", "agy work")
    git(human, "push", "origin", f"w:refs/heads/coxd-agy/{tid}")
    return git(human, "rev-parse", "HEAD").strip()


gate_calls = []
GREEN = {"passed": True, "steps": {"test": "ok"}}
RED = {"passed": False, "failing": "test", "reason": "1 failed: test_thing", "steps": {}}
gate_result = [GREEN]


def fake_gate(wt, entry, path="full"):
    gate_calls.append(wt)
    return gate_result[0]


gate.run_gate = fake_gate
registry.get_or_scout = lambda r, w: {}
verdict = [lane.ReviewOutcome("reviewed", "approve", [], 0.02)]


async def fake_review(diff, model, emit, effort="medium", *, worktree):
    return verdict[0]


lane.review = fake_review
FINDINGS = [
    {"severity": "low", "summary": "rename x"},
    {"severity": "high", "summary": "no input check", "file": "b.py", "line": 1},
]

# ---- detection fires once; red gate -> waiting_agy round 2 ---------------------------
t1 = new_agy_task()
check("no new head -> nothing queued", agy_handoff.scan() == [])
sha1 = human_push(t1, "y = 2\n")
check("new head queued once", agy_handoff.scan() == [t1])
check("no double detection", agy_handoff.scan() == [])
check("claimed as queued/agy-pickup", store.get_task(t1)["state"] == "queued"
      and store.get_task(t1)["reason"] == agy_handoff.AGY_PICKUP)
gate_result[0] = RED
store.set_state(t1, "working")  # what _runner does when it claims
asyncio.run(loop.run_task(t1))
t = store.get_task(t1)
check("worktree at agy head", git(t["worktree"], "rev-parse", "HEAD").strip() == sha1)
check("gate ran once", len(gate_calls) == 1)
check("red gate -> waiting_agy", t["state"] == "waiting_agy")
check("plan is gate summary",
      "test_thing" in t["plan_md"] and "The gate failed at test" in t["plan_md"])
c = TestClient(board.app, base_url="http://localhost:8791")
check("round 2 on plan endpoint", c.get(f"/api/task/{t1}/plan").json()["round"] == 2)
check("same head not re-run", agy_handoff.scan() == [])
check("agy-next notified", pings[-1][1] == f"Run on your Mac: agy-next {t1}")

# ---- green gate + findings -> needs_choice ---------------------------------------------
gate_result[0] = GREEN
verdict[0] = lane.ReviewOutcome("reviewed", "fix", FINDINGS, 0.02)
sha2 = human_push(t1, "y = 3\n")
check("second push queued", agy_handoff.scan() == [t1])
store.set_state(t1, "working")
asyncio.run(loop.run_task(t1))
check("findings -> needs_choice", store.get_task(t1)["state"] == "needs_choice")
row = next(r for r in c.get("/api/tasks").json()["tasks"] if r["id"] == t1)
check("board card: severity order + needs_you",
      row["needs_you"]
      and [f["severity"] for f in row["choice"]["findings"]] == ["critical", "minor"]
      and not row["choice"]["capped"])
check("bad choice refused",
      c.post(f"/api/task/{t1}/agy-choice", json={"choice": "x"}).status_code == 400)

# ---- Send to agy: findings are the next plan round --------------------------------------
r = c.post(f"/api/task/{t1}/agy-choice", json={"choice": "send"})
t = store.get_task(t1)
check("send -> waiting_agy", r.status_code == 200 and t["state"] == "waiting_agy")
check("findings are the plan", "no input check" in t["plan_md"] and
      t["plan_md"].index("no input check") < t["plan_md"].index("rename x"))
check("round 3", c.get(f"/api/task/{t1}/plan").json()["round"] == 3)
check("agy-next command on card", next(x for x in c.get("/api/tasks").json()["tasks"]
      if x["id"] == t1)["agy_round"] == 3)
check("double click refused",
      c.post(f"/api/task/{t1}/agy-choice", json={"choice": "send"}).status_code == 400)

# ---- round cap: two fix rounds used, third red gate -> needs_human -----------------------
gate_result[0] = RED
human_push(t1, "y = 4\n")
agy_handoff.scan()
store.set_state(t1, "working")
asyncio.run(loop.run_task(t1))
check("round cap -> needs_human", store.get_task(t1)["state"] == "needs_human"
      and store.get_task(t1)["reason"] == "gate-red")

# ---- approve -> PR from the agy branch with [agy] title -----------------------------------
gate_result[0] = GREEN
verdict[0] = lane.ReviewOutcome("reviewed", "approve", [], 0.02)
t2 = new_agy_task()
shaB = human_push(t2, "z = 1\n")
agy_handoff.scan()
store.set_state(t2, "working")
real_run = subprocess.run
gh_calls = []


def fake_run(argv, *a, **k):
    if argv and argv[0] == "gh":
        gh_calls.append(argv)
        return SimpleNamespace(
            returncode=0, stdout="https://github.com/acme/widgets/pull/7\n", stderr="")
    return real_run(argv, *a, **k)


ship._has_gh_remote = lambda p: True
ship.subprocess.run = fake_run
asyncio.run(loop.run_task(t2))
ship.subprocess.run = real_run
t = store.get_task(t2)
pr = next(c_ for c_ in gh_calls if c_[:3] == ["gh", "pr", "create"])
check("approve -> pr_ready", t["state"] == "pr_ready" and t["pr_url"].endswith("/pull/7"))
check("PR head is agy branch", pr[pr.index("--head") + 1] == f"coxd-agy/{t2}")
check("PR title tagged", pr[pr.index("--title") + 1].startswith("[agy] "))
check("agy branch untouched by ship",
      git(origin, "rev-parse", f"refs/heads/coxd-agy/{t2}").strip() == shaB)
no_coxd = real_run(["git", "rev-parse", "--verify", "-q", f"refs/heads/coxd/{t2}"], cwd=origin)
check("no coxd/ branch pushed", no_coxd.returncode != 0)

# ---- Let Claude fix: fresh worker session, normal tail, pushes onto the agy branch ------
verdict[0] = lane.ReviewOutcome("reviewed", "fix", FINDINGS, 0.02)
t3 = new_agy_task()
human_push(t3, "w = 1\n")
agy_handoff.scan()
store.set_state(t3, "working")
asyncio.run(loop.run_task(t3))
check("t3 needs_choice", store.get_task(t3)["state"] == "needs_choice")
worker_calls = []


async def fake_worker(wt, prompt, model, emit, resume=None, effort="medium"):
    worker_calls.append((resume, prompt))
    (Path(wt) / "c.py").write_text("c = 1\n")
    git(wt, "add", ".")
    git(wt, "commit", "-m", "claude fix")
    return SimpleNamespace(session_id="s-1", cost=0.5, summary="fixed", is_error=False,
                           quota_reset_at=None)


lane.run_worker = fake_worker
verdict[0] = lane.ReviewOutcome("reviewed", "approve", [], 0.02)
r = c.post(f"/api/task/{t3}/agy-choice", json={"choice": "claude"})
check("claude choice queued", r.status_code == 200 and store.get_task(t3)["state"] == "queued")
store.set_state(t3, "working")  # runner claim clears the reason
gh_calls.clear()
ship.subprocess.run = fake_run
asyncio.run(loop.run_task(t3))
ship.subprocess.run = real_run
check("fresh session, findings in prompt", worker_calls and worker_calls[0][0] is None
      and "no input check" in worker_calls[0][1])
check("t3 pr_ready from agy branch", store.get_task(t3)["state"] == "pr_ready"
      and any(g[g.index("--head") + 1] == f"coxd-agy/{t3}" for g in gh_calls if "create" in g))
check("claude commit on the agy branch",
      git(origin, "log", "--format=%s", "-1", f"refs/heads/coxd-agy/{t3}").strip() == "claude fix")
check("claude-fix cost on card data", board._last_claude_fix_cost(store.events(t3)) == 0.5)

# ---- agy_send_enabled OFF (the default): fixes always go to Claude ------------------------
check("default (no key, no env) is off", agy_handoff.send_enabled("nosuchrepo") is False)
registry.save("widgets", {})
check("empty registry entry is off", agy_handoff.send_enabled("widgets") is False)
registry.save("widgets", {"agy_send_enabled": "false"})
check("string false is off", agy_handoff.send_enabled("widgets") is False)
registry.save("widgets", {"agy_send_enabled": True})
check("registry true is on", agy_handoff.send_enabled("widgets") is True)

# needs_choice card (e.g. from before the flag flipped): no send button, send -> 400
verdict[0] = lane.ReviewOutcome("reviewed", "fix", FINDINGS, 0.02)
gate_result[0] = GREEN
t5 = new_agy_task()
human_push(t5, "o = 1\n")
agy_handoff.scan()
store.set_state(t5, "working")
asyncio.run(loop.run_task(t5))
check("flag on: t5 needs_choice", store.get_task(t5)["state"] == "needs_choice")
row = next(x for x in c.get("/api/tasks").json()["tasks"] if x["id"] == t5)
check("flag on: send button offered", row["choice"]["send_enabled"] is True)
registry.save("widgets", {})
row = next(x for x in c.get("/api/tasks").json()["tasks"] if x["id"] == t5)
check("flag off: send button absent in payload", row["choice"]["send_enabled"] is False)
r = c.post(f"/api/task/{t5}/agy-choice", json={"choice": "send"})
check("flag off: send -> 400 disabled by owner", r.status_code == 400
      and "disabled by owner" in r.json()["error"]
      and store.get_task(t5)["state"] == "needs_choice")

# red gate after an agy push -> Claude fix round (no waiting_agy), counted against the cap
worker_calls.clear()
gate_calls.clear()
verdict[0] = lane.ReviewOutcome("reviewed", "approve", [], 0.02)
gate_results = [RED, GREEN]  # red after the agy push, green after Claude's fix


def seq_gate(wt, entry, path="full"):
    gate_calls.append(wt)
    return gate_results.pop(0) if len(gate_results) > 1 else gate_results[0]


gate.run_gate = seq_gate
t6 = new_agy_task()
human_push(t6, "r = 1\n")
agy_handoff.scan()
store.set_state(t6, "working")
gh_calls.clear()
ship.subprocess.run = fake_run
asyncio.run(loop.run_task(t6))
ship.subprocess.run = real_run
evs6 = store.events(t6)
check("gate-red -> Claude worker, fresh session, gate text in prompt",
      len(worker_calls) == 1 and worker_calls[0][0] is None
      and "test_thing" in worker_calls[0][1])
check("gate-red: no agy round opened", sum(1 for e in evs6 if e["kind"] == "plan") == 1
      and store.get_task(t6)["state"] == "pr_ready")
check("gate-red round counted against cap", agy_handoff.rounds_used(t6) == 1
      and [e["data"] for e in evs6 if e["kind"] == "fix-round"]
      == [{"via": "claude", "source": "gate"}])

# round cap still applies when already at the cap
gate.run_gate = lambda wt, entry, path="full": RED
t7 = new_agy_task()
for _ in range(agy_handoff.MAX_FIX_ROUNDS):
    agy_handoff.record_claude_round(t7, "gate")
human_push(t7, "s = 1\n")
agy_handoff.scan()
store.set_state(t7, "working")
worker_calls.clear()
asyncio.run(loop.run_task(t7))
check("cap: red gate -> needs_human, no Claude round", not worker_calls
      and store.get_task(t7)["state"] == "needs_human"
      and store.get_task(t7)["reason"] == "gate-red")

# review findings (verdict fix) -> straight to Claude, no needs_choice
gate.run_gate = fake_gate
gate_result[0] = GREEN
verdicts = [lane.ReviewOutcome("reviewed", "fix", FINDINGS, 0.02),
            lane.ReviewOutcome("reviewed", "approve", [], 0.02)]


async def seq_review(diff, model, emit, effort="medium", *, worktree):
    return verdicts.pop(0) if len(verdicts) > 1 else verdicts[0]


lane.review = seq_review
t8 = new_agy_task()
human_push(t8, "u = 1\n")
agy_handoff.scan()
store.set_state(t8, "working")
worker_calls.clear()
ship.subprocess.run = fake_run
asyncio.run(loop.run_task(t8))
ship.subprocess.run = real_run
check("review-fix -> Claude worker with findings, never needs_choice",
      len(worker_calls) == 1 and "no input check" in worker_calls[0][1]
      and store.get_task(t8)["state"] == "pr_ready")
check("review-fix round counted", agy_handoff.rounds_used(t8) == 1)

# review findings at the cap -> needs_human
verdicts[:] = [lane.ReviewOutcome("reviewed", "fix", FINDINGS, 0.02)]
t9 = new_agy_task()
for _ in range(agy_handoff.MAX_FIX_ROUNDS):
    agy_handoff.record_claude_round(t9, "review")
human_push(t9, "v = 1\n")
agy_handoff.scan()
store.set_state(t9, "working")
worker_calls.clear()
asyncio.run(loop.run_task(t9))
check("cap: review-fix -> needs_human", not worker_calls
      and store.get_task(t9)["state"] == "needs_human")

registry.save("widgets", {"agy_send_enabled": True})

# ---- quota: review waits, never falls back -----------------------------------------------
t4 = new_agy_task()
human_push(t4, "q = 1\n")
agy_handoff.scan()
store.set_state(t4, "working")


async def quota_review(diff, model, emit, effort="medium", *, worktree):
    return lane.ReviewOutcome("reviewed", None, [], 0.0, quota_reset_at=9e9)


lane.review = quota_review
try:
    asyncio.run(loop.run_task(t4))
    check("quota raises QuotaWait", False)
except quota.QuotaWait as q:
    quota.park(t4, q.reset_at, q.stage)
    row = next(x for x in c.get("/api/tasks").json()["tasks"] if x["id"] == t4)
    check("parked, banner time set", row["state"] == "waiting_quota" and row["quota_wait_at"])

# ---- plain coxd-ready task is not touched by the scan --------------------------------------
store.create_task("ready-1", "widgets", "b", "/wt", repo_path=str(repo))
store.set_state("ready-1", "waiting_agy")
check("non-agy lane ignored by scan", "ready-1" not in agy_handoff.scan())

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)

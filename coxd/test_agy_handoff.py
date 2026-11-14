"""agy handoff lane 1/4: label -> lane, plan step, branch, waiting_agy, plan endpoint.

    cd coxd && python test_agy_handoff.py
(`gh` mocked, `lane.plan` mocked, real local git repo + bare origin; invented data only.)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-agy-")
os.environ["COXD_WORKTREES"] = tempfile.mkdtemp(prefix="coxd-agy-wt-")

import board  # noqa: E402
import ci_triage  # noqa: E402
import intake  # noqa: E402
import lane  # noqa: E402
import loop  # noqa: E402
import notify  # noqa: E402
import pii  # noqa: E402
import registry  # noqa: E402
import repos  # noqa: E402
import store  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

fails = 0
open(pii.denylist_path(), "w").write("Zebulon Quux\n")


def check(name, cond):
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}).stdout


root = Path(tempfile.mkdtemp(prefix="coxd-agy-git-"))
origin, repo = root / "origin.git", root / "widgets"
git(root, "init", "--bare", "-b", "main", str(origin))
git(root, "init", "-b", "main", str(repo))
(repo / "a.py").write_text("x = 1\n")
git(repo, "add", ".")
git(repo, "commit", "-m", "init")
git(repo, "remote", "add", "origin", str(origin))
git(repo, "push", "origin", "main")

SLUG = "acme/widgets"
URL = f"https://github.com/{SLUG}/issues/"
issues: dict[int, dict] = {}


def add(n, label):
    issues[n] = {"number": n, "title": f"Add thing number {n}", "body": "Do a thing.", "url": URL + str(n),
                 "createdAt": f"2026-01-0{n}T00:00:00Z", "state": "OPEN",
                 "labels": [{"name": label}], "label": label}


def fake_gh(a):
    if a[:2] == ["label", "list"]:
        return 0, "[]"
    if a[0] == "label":
        return 0, ""
    if a[:2] == ["issue", "list"]:
        lb = a[a.index("--label") + 1]
        return 0, json.dumps([i for i in issues.values()
                              if lb in [x["name"] for x in i["labels"]]])
    if a[:2] == ["issue", "edit"]:
        i = issues[int(a[2])]
        names = [x["name"] for x in i["labels"]]
        for k, v in enumerate(a):
            if v == "--remove-label":
                names = [x for x in names if x != a[k + 1]]
            if v == "--add-label":
                names.append(a[k + 1])
        i["labels"] = [{"name": n} for n in names]
        return 0, ""
    if a[:2] == ["issue", "view"]:
        return 0, json.dumps({"state": "OPEN", "labels": issues[int(a[2])]["labels"]})
    if a[0] == "api" and a[1].endswith("/timeline"):
        n = int(a[1].split("/")[4])
        return 0, json.dumps([[{"event": "labeled", "label": {"name": issues[n]["label"]},
                                "created_at": "2026-03-01T00:00:00Z",
                                "actor": {"login": "alice", "type": "User"}}]])
    if a[0] == "api" and a[1] == "graphql":
        return 0, json.dumps({"data": {"repository": {"issue": {}}}})
    if a[0] == "api" and "/collaborators/" in a[1]:
        return 0, json.dumps({"permission": "write"})
    raise AssertionError(a)


intake._gh = fake_gh
ci_triage.repo_slug = lambda p: SLUG
repos.list_repos = lambda: [{"name": "widgets", "path": str(repo)}]
registry.save("widgets", {"intake": True})
pings: list[tuple] = []
notify.notify_async = lambda title, msg, priority="default": pings.append((title, msg))

add(1, "coxd-agy")
add(2, "coxd-ready")
check("poll takes both", intake.poll_once() == 2)
tasks = {t["brief"].split("GitHub issue: ")[1].split("/")[-1]: t for t in store.list_tasks()}
agy, ready = tasks["1"], tasks["2"]
check("agy task lane", agy["lane"] == "agy-handoff")
check("coxd-ready lane null", ready["lane"] is None)
check("agy label swapped", [x["name"] for x in issues[1]["labels"]] == ["coxd-working"])

calls: list[tuple] = []


async def fake_plan(brief, model, emit, effort="medium", *, worktree):
    calls.append((model, effort, Path(worktree)))
    return lane.PlanOutcome("planned", "## Files\n- a.py\n", 0.01)


lane.plan = fake_plan
tid = agy["id"]
res = asyncio.run(loop.run_task(tid))
t = store.get_task(tid)
base = git(repo, "rev-parse", "main").strip()
check("state waiting_agy", res == "waiting_agy" and t["state"] == "waiting_agy")
check("planner model via roles", bool(calls) and calls[0][0] == "claude-sonnet-5-5")
check("plan stored", t["plan_md"].startswith("## Files") and t["base_sha"] == base)
check("plan event", any(e["kind"] == "plan" for e in store.events(tid)))
remote = git(origin, "rev-parse", f"refs/heads/coxd-agy/{tid}").strip()
check("remote branch at base sha", remote == base)
check("notified", bool(pings) and pings[-1][1] == f"Run on your Mac: agy-next {tid}")

c = TestClient(board.app, base_url="http://localhost:8791")
r = c.get(f"/api/task/{tid}/plan").json()
check("plan endpoint shape", r == {"task_id": tid, "repo": "widgets",
                                   "branch": f"coxd-agy/{tid}", "base_sha": base,
                                   "round": 1, "plan_md": t["plan_md"]})
check("plan 404 for ready task", c.get(f"/api/task/{ready['id']}/plan").status_code == 404)
row = next(x for x in c.get("/api/tasks").json()["tasks"] if x["id"] == tid)
check("board needs_you + cmd", row["needs_you"] and row["agy_cmd"] == f"agy-next {tid}")

# plan failure -> needs_human, no branch
add(3, "coxd-agy")
intake.poll_once()
t3 = next(x for x in store.list_tasks() if x["brief"].endswith(URL + "3"))


async def bad_plan(*a, **k):
    return lane.PlanOutcome("plan-error", None, None)


lane.plan = bad_plan
asyncio.run(loop.run_task(t3["id"]))
check("plan-error -> needs_human", store.get_task(t3["id"])["state"] == "needs_human")
check("no branch on failure", subprocess.run(
    ["git", "rev-parse", "--verify", "-q", f"refs/heads/coxd-agy/{t3['id']}"],
    cwd=origin).returncode != 0)

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)

"""agy queue 6/8: refresh a stale base (or re-plan) when a blocked task unblocks.

    cd coxd && python test_agy_refresh.py
(real local git + bare origin; lane.plan mocked; invented data only; agy never run.)
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-refresh-")
os.environ["COXD_WORKTREES"] = tempfile.mkdtemp(prefix="coxd-refresh-wt-")
os.environ.pop("COXD_WORKER_USER", None)  # sandbox.wrap is a no-op off the NAS
tempfile.tempdir = tempfile.mkdtemp(prefix="coxd-refresh-tmp-")

import agy_handoff  # noqa: E402
import lane  # noqa: E402
import loop  # noqa: E402
import notify  # noqa: E402
import registry  # noqa: E402
import store  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True,
                          env=ENV).stdout.strip()


root = Path(tempfile.mkdtemp(prefix="coxd-refresh-git-"))
origin, repo, human = root / "origin.git", root / "widgets", root / "human"
git(root, "init", "--bare", "-b", "main", str(origin))
git(root, "init", "-b", "main", str(repo))
(repo / "a.py").write_text("x = 1\n")
(repo / "c.py").write_text("z = 1\n")
git(repo, "add", ".")
git(repo, "commit", "-m", "init")
git(repo, "remote", "add", "origin", str(origin))
git(repo, "push", "origin", "main")
git(root, "clone", str(origin), str(human))
registry.save("widgets", {})
notify.notify_async = lambda *a, **k: None

# no agy invocation of any kind: fail loudly if a subprocess names it
_real_run = subprocess.run
agy_calls: list = []


def spy_run(args, *a, **kw):
    if "agy" in (args if isinstance(args, str) else " ".join(map(str, args))).split():
        agy_calls.append(args)
    return _real_run(args, *a, **kw)


subprocess.run = spy_run


def main_commit(fname, text):
    """Advance origin/main from the 'human' clone with an unrelated commit."""
    git(human, "fetch", "-q", "origin")
    git(human, "checkout", "-q", "-B", "m", "origin/main")
    (human / fname).write_text(text)
    git(human, "add", ".")
    git(human, "commit", "-m", f"change {fname}")
    git(human, "push", "-q", "origin", "m:refs/heads/main")
    return git(human, "rev-parse", "HEAD")


n = [0]
PLAN = "## Files\n- a.py\n"


def new_task(files=("a.py",), plan=PLAN):
    """An agy task parked at waiting_agy with its branch cut from current origin/main."""
    n[0] += 1
    tid = f"task-test-{n[0]}"
    git(repo, "fetch", "-q", "origin")
    base = git(repo, "rev-parse", "origin/main")
    git(repo, "push", "-q", "origin", f"{base}:refs/heads/coxd-agy/{tid}")
    wt = Path(os.environ["COXD_WORKTREES"]) / tid
    git(repo, "worktree", "add", "-q", "-b", f"coxd/{tid}", str(wt), base)
    store.create_task(tid, "widgets", "Add thing", str(wt), repo_path=str(repo),
                      intent="Add thing", lane="agy-handoff")
    store.set_plan(tid, plan, base)
    store.append_event(tid, "plan", {"plan_md": plan, "base_sha": base, "round": 1,
                                     "files": list(files)})
    store.set_state(tid, "waiting_agy", f"run agy-next {tid}")
    return tid


def remote_branch(tid):
    return git(repo, "ls-remote", "origin", f"refs/heads/coxd-agy/{tid}").split()[0]


def kinds(tid):
    return [e["kind"] for e in store.events(tid)]


# ---- untouched branch moves to main; plan_md kept -------------------------------------
t1 = new_task()
old1 = store.get_task(t1)["base_sha"]
new_main = main_commit("b.py", "y = 1\n")  # unrelated to the planned a.py
check("refreshed", agy_handoff.refresh_unblocked(store.get_task(t1)) == "refreshed")
check("remote branch == main", remote_branch(t1) == new_main)
tk = store.get_task(t1)
check("base_sha updated", tk["base_sha"] == new_main != old1)
check("plan_md kept", tk["plan_md"] == PLAN)
check("still waiting_agy", tk["state"] == "waiting_agy")
ev = [e for e in store.events(t1) if e["kind"] == "base-refreshed"]
check("base-refreshed emitted", len(ev) == 1 and ev[0]["data"]["old"] == old1
      and ev[0]["data"]["new"] == new_main)
check("second call is a no-op (up to date)",
      agy_handoff.refresh_unblocked(store.get_task(t1)) == "skipped"
      and kinds(t1).count("base-refreshed") == 1)

# ---- human commits: left alone ---------------------------------------------------------
t2 = new_task()
git(human, "fetch", "-q", "origin")
git(human, "checkout", "-q", "-B", "w", f"origin/coxd-agy/{t2}")
(human / "h.py").write_text("h = 1\n")
git(human, "add", ".")
git(human, "commit", "-q", "-m", "human work")
git(human, "push", "-q", "origin", f"w:refs/heads/coxd-agy/{t2}")
human_head = git(human, "rev-parse", "HEAD")
base2 = store.get_task(t2)["base_sha"]
main_commit("d.py", "d = 1\n")
check("human commits skipped", agy_handoff.refresh_unblocked(store.get_task(t2)) == "skipped")
check("branch untouched", remote_branch(t2) == human_head)
check("base_sha untouched", store.get_task(t2)["base_sha"] == base2)
check("no refresh event", "base-refreshed" not in kinds(t2) and "agy-replan" not in kinds(t2))

# ---- planned file changed on main: re-plan --------------------------------------------
t3 = new_task()
base3 = store.get_task(t3)["base_sha"]
main_commit("a.py", "x = 2\n")
check("replan", agy_handoff.refresh_unblocked(store.get_task(t3)) == "replan")
tk = store.get_task(t3)
check("queued with agy-replan", tk["state"] == "queued" and tk["reason"] == "agy-replan")
check("agy-replan event", "agy-replan" in kinds(t3))
check("branch not refreshed", remote_branch(t3) == base3 and "base-refreshed" not in kinds(t3))
check("replan pending marker", agy_handoff.replan_pending(t3))

# ---- re-plan run: new plan round, force-pushed branch, waiting_agy --------------------
plan_calls: list = []


async def fake_plan(brief, model, emit, effort="medium", *, worktree):
    plan_calls.append(Path(worktree))
    return lane.PlanOutcome("planned", "## Files\n- a.py\n- new.py\n", 0.01,
                            files=["a.py", "new.py"])


lane.plan = fake_plan
store.set_state(t3, "working")  # the supervisor claims the task, clearing the reason
res = asyncio.run(loop.run_task(t3))
tk = store.get_task(t3)
git(repo, "fetch", "-q", "origin")
main_now = git(repo, "rev-parse", "origin/main")
check("replan run -> waiting_agy", res == "waiting_agy" and tk["state"] == "waiting_agy")
check("planner ran once", len(plan_calls) == 1)
plans = [e for e in store.events(t3) if e["kind"] == "plan"]
check("plan round 2", len(plans) == 2 and plans[-1]["data"]["round"] == 2)
check("branch force-pushed to main", remote_branch(t3) == main_now)
check("base_sha is main", tk["base_sha"] == main_now)
check("no longer pending", not agy_handoff.replan_pending(t3))

# ---- push failure leaves everything unchanged ------------------------------------------
t4 = new_task()
base4 = store.get_task(t4)["base_sha"]
main_commit("e.py", "e = 1\n")
hook = origin / "hooks" / "pre-receive"
hook.write_text("#!/bin/sh\nexit 1\n")  # the remote rejects every push
hook.chmod(0o755)
check("push failure -> error", agy_handoff.refresh_unblocked(store.get_task(t4)) == "error")
check("base_sha unchanged on failure", store.get_task(t4)["base_sha"] == base4)
check("agy-error emitted", "agy-error" in kinds(t4) and "base-refreshed" not in kinds(t4))
hook.unlink()

# ---- notify_queue: only newly unblocked tasks are refreshed ----------------------------
t5 = new_task()
t6 = new_task()
main_commit("f.py", "f = 1\n")
state = agy_handoff._load_notify_state()
state["blocked"] = {t5: ["#99"]}  # t5 was blocked last pass; t6 was not
agy_handoff._save_notify_state(state)
agy_handoff.queue_inputs = lambda: (
    store.list_tasks(),
    {t["id"]: store.events(t["id"]) for t in store.list_tasks() if t.get("lane")},
    {}, {})
old6 = store.get_task(t6)["base_sha"]
agy_handoff.notify_queue(now=1000.0)
check("unblocked task refreshed by notify_queue", "base-refreshed" in kinds(t5))
check("never-blocked task untouched", store.get_task(t6)["base_sha"] == old6)

# ---- coxd-ready (non-agy) tasks are untouched ------------------------------------------
store.create_task("ready-1", "widgets", "b", "/wt", repo_path=str(repo))
store.set_state("ready-1", "waiting_agy")
store.set_plan("ready-1", "p", "0" * 40)
check("non-agy lane skipped", agy_handoff.refresh_unblocked(store.get_task("ready-1")) == "skipped")

check("agy never invoked", agy_calls == [])
print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)

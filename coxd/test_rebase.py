"""Auto-rebase of open coxd PRs after a merge. Standalone (temp local repos + a local
bare "remote", no network, invented data):
    cd coxd && COXD_HOME=$(mktemp -d) python test_rebase.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-rb-")
os.environ.pop("COXD_WORKER_USER", None)
os.environ["COXD_GIT_NAME"] = "coxd-bot"
os.environ["COXD_GIT_EMAIL"] = "bot@example.invalid"
tempfile.tempdir = tempfile.mkdtemp(prefix="coxd-tmp-")

import factory  # noqa: E402
import gate  # noqa: E402
import rebase  # noqa: E402
import ship  # noqa: E402
import store  # noqa: E402

fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


def git(cwd, *a, who=("coxd-bot", "bot@example.invalid")):
    env = {**os.environ, "GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1],
           "GIT_COMMITTER_NAME": who[0], "GIT_COMMITTER_EMAIL": who[1]}
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def commit(cwd, fname, text, who=("coxd-bot", "bot@example.invalid")):
    (Path(cwd) / fname).write_text(text)
    git(cwd, "add", ".", who=who)
    git(cwd, "commit", "-q", "-m", f"edit {fname}", who=who)


root = Path(tempfile.mkdtemp(prefix="coxd-rbrepo-"))
remote = root / "remote.git"
git(root, "init", "-q", "--bare", "-b", "main", str(remote))
git(remote, "config", "receive.denyNonFastForwards", "false")
repo = root / "repo"
git(root, "clone", "-q", str(remote), str(repo))
git(repo, "checkout", "-q", "-b", "main")
commit(repo, "a.txt", "one\n")
commit(repo, "b.txt", "one\n")
git(repo, "push", "-q", "-u", "origin", "main")


def make_task(tid, fname, text, who=("coxd-bot", "bot@example.invalid")):
    wt = root / "wt" / tid
    git(repo, "worktree", "add", "-q", "-b", f"coxd/{tid}", str(wt), "origin/main")
    commit(wt, fname, text, who=who)
    git(repo, "push", "-q", "origin", f"coxd/{tid}")
    git(repo, "fetch", "-q")
    store.create_task(tid, "rbrepo", "brief", str(wt), repo_path=str(repo))
    store.set_session(tid, "sess-" + tid)
    store.set_pr_url(tid, f"https://github.com/o/r/pull/{abs(hash(tid)) % 900}")
    store.set_state(tid, "pr_ready")


make_task("clean", "c.txt", "mine\n")
make_task("conf", "a.txt", "conflicting\n")
make_task("foreign", "d.txt", "x\n", who=("someone", "else@example.invalid"))
make_task("red", "e.txt", "x\n")

# another PR lands on main
commit(repo, "a.txt", "changed upstream\n")
git(repo, "push", "-q", "origin", "main")

rebase.ci_triage.repo_slug = lambda p: "o/r"
rebase._pr_open = lambda url, slug: True
calls: list = []
gate_ok = {"v": True}


def fake_gate(wt, entry, path="full"):
    calls.append("gate")
    return {"passed": gate_ok["v"], "failing": None if gate_ok["v"] else "test", "steps": {}}


def fake_pii(tid, t, rp, branch, wt, ctx=None):
    calls.append("pii")
    if ctx is not None:
        ctx["sha"] = git(rp, "rev-parse", f"refs/heads/{branch}")
    return None


gate.run_gate = fake_gate
ship._pii_gate = fake_pii
pushes: list = []
real_run = subprocess.run


def spy_run(cmd, *a, **k):
    if isinstance(cmd, list) and cmd[:2] == ["git", "push"]:
        pushes.append(cmd)
    return real_run(cmd, *a, **k)


rebase.subprocess.run = spy_run


def kinds(tid):
    return [e["kind"] for e in store.events(tid)]


def remote_sha(tid):
    return git(remote, "rev-parse", f"refs/heads/coxd/{tid}")


before = {t: remote_sha(t) for t in ("clean", "conf", "foreign", "red")}

# paused factory → nothing happens
factory.set_paused(True)
rebase.mark_merged("rbrepo")
rebase.process_pending(0, 2)
check("paused: no events, no push, still pending",
      not any(k.startswith("rebase") for t in before for k in kinds(t)) and not pushes
      and "rbrepo" in rebase._pending)
factory.set_paused(False)

# concurrency full → deferred
rebase.process_pending(2, 2)
check("concurrency full: deferred", not pushes and "rbrepo" in rebase._pending)

gate_ok["v"] = False  # applies to every clean-rebasing task; flip back for "clean" below
# run "red" alone first: only it should reach the gate failure
for t in ("clean", "conf", "foreign"):
    store.set_state(t, "archived_tmp") if False else None
sha = git(repo, "rev-parse", "origin/main")
who = rebase.identity(repo)
rebase.rebase_task(store.get_task("red"), "o/r", "main", sha, who)
red = store.get_task("red")
check("gate red: needs_human/gate-red, no push",
      red["state"] == "needs_human" and red["reason"] == "gate-red" and not pushes
      and remote_sha("red") == before["red"])
gate_ok["v"] = True

calls.clear()
rebase.process_pending(0, 2)
check("clean: gate + pii re-run", calls.count("gate") == 1 and calls.count("pii") == 1)
c = store.get_task("clean")
ev = [e for e in store.events("clean") if e["kind"] == "rebased"]
check("clean: rebased event, still pr_ready", len(ev) == 1 and c["state"] == "pr_ready")
check("clean: pushed with --force-with-lease to own branch only",
      len(pushes) == 1 and pushes[0][2].startswith("--force-with-lease=refs/heads/coxd/clean:")
      and pushes[0][-1].endswith(":refs/heads/coxd/clean") and "--force" not in pushes[0])
check("clean: remote branch now contains main",
      git(repo, "merge-base", "--is-ancestor", "origin/main", "refs/heads/coxd/clean") == ""
      and remote_sha("clean") != before["clean"])
check("main untouched", git(remote, "rev-parse", "refs/heads/main") == sha)

cf = store.get_task("conf")
check("conflict: aborted, resume queued with note",
      cf["state"] == "queued" and cf["reason"] == "human-resume"
      and "resolve the conflicts" in [e for e in store.events("conf")
                                       if e["kind"] == "resume-request"][-1]["data"]["note"]
      and "rebase-conflict" in kinds("conf")
      and not (Path(cf["worktree"]) / ".git").is_dir()
      and git(cf["worktree"], "status", "--porcelain") == ""
      and remote_sha("conf") == before["conf"])
fo = [e for e in store.events("foreign") if e["kind"] == "rebase-skipped"]
check("foreign: skipped, no push",
      len(fo) == 1 and fo[0]["data"]["reason"] == "foreign commits"
      and remote_sha("foreign") == before["foreign"] and len(pushes) == 1)
check("sweep done → no longer pending", "rbrepo" not in rebase._pending)

# same target sha again → no second attempt
store.set_state("conf", "pr_ready")
n_events = {t: len(store.events(t)) for t in before}
pushes.clear()
calls.clear()
rebase.mark_merged("rbrepo")
rebase.process_pending(0, 2)
check("same target sha: nothing re-attempted",
      {t: len(store.events(t)) for t in before} == n_events and not pushes and not calls)

print([(e["kind"], e["data"]) for e in store.events("clean")]) if fails else None
print("OK" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)

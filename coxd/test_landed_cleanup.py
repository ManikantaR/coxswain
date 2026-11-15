"""landed.check_and_cleanup_merged: pr_ready + needs_human landing, lane-correct branch delete.

    cd coxd && .venv/bin/python test_landed_cleanup.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-landedc-")

import landed  # noqa: E402
import store  # noqa: E402

res = []


def check(name, cond):
    res.append(cond)
    print(("PASS" if cond else "FAIL"), name)


SLUG = "o/acme-widgets"
runs: list = []
closed: list = []
streaks: list = []
removed: list = []

landed._run = lambda args, cwd=None: runs.append(args) or type(  # type: ignore[assignment]
    "P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
landed._pr_merged = lambda url, slug: (url.endswith("/1"), None)  # type: ignore[assignment]
landed._close_issue_if_open = lambda b, s, u: closed.append(u)  # type: ignore[assignment]
landed._record_streak = lambda t, s: streaks.append(t["id"])  # type: ignore[assignment]
landed._maybe_deploy = lambda t, s: None  # type: ignore[assignment]
landed.ci_triage.repo_slug = lambda p: SLUG  # type: ignore[assignment]
landed.worktree.remove = lambda rp, wt, br: removed.append(br)  # type: ignore[assignment]


def mk(tid, state, lane=None, pr=1):
    store.create_task(tid, "acme-widgets", "brief", f"/wt/{tid}",
                      repo_path="/repo/acme-widgets", lane=lane)
    store.set_pr_url(tid, f"https://github.com/{SLUG}/pull/{pr}")
    store.set_state(tid, state, "x")


def deleted(branch):
    return ["git", "push", "origin", "--delete", branch] in runs


def cleanup_event(tid):
    return [e for e in store.events(tid) if e["kind"] == "auto-cleanup"]


mk("pr1", "pr_ready")
mk("nh1", "needs_human")
mk("nh2", "needs_human", pr=2)  # PR not merged -> untouched
mk("ag1", "needs_human", lane="agy-handoff")
mk("ag2", "pr_ready", lane="agy-handoff")
landed.check_and_cleanup_merged()

check("pr_ready merged -> landed", store.get_task("pr1")["state"] == "landed")
check("pr_ready still records streak", "pr1" in streaks)
check("pr_ready deletes coxd/<id>", deleted("coxd/pr1") and "coxd/pr1" in removed)
check("pr_ready event from", cleanup_event("pr1")[0]["data"].get("from") == "pr_ready")
check("needs_human event from", cleanup_event("nh1")[0]["data"].get("from") == "needs_human")

check("needs_human merged -> landed", store.get_task("nh1")["state"] == "landed")
check("needs_human closes issue", "https://github.com/o/acme-widgets/pull/1" in closed)
check("needs_human: no streak", "nh1" not in streaks)
check("needs_human deletes coxd/<id>", deleted("coxd/nh1"))
check("needs_human unmerged untouched", store.get_task("nh2")["state"] == "needs_human")

check("agy task deletes coxd-agy/<id>", deleted("coxd-agy/ag1") and deleted("coxd-agy/ag2"))
check("agy task never deletes coxd/<id> remotely",
      not deleted("coxd/ag1") and not deleted("coxd/ag2"))
check("agy local branch still coxd/<id>", "coxd/ag1" in removed)
check("agy pr_ready keeps streak", "ag2" in streaks and "ag1" not in streaks)

sys.exit(0 if all(res) else 1)

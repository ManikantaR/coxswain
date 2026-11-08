"""Tests for the per-repo STATUS.md.

    cd coxd && .venv/bin/python test_status.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-status-")

import cli  # noqa: E402
import status  # noqa: E402
import store  # noqa: E402

res = []


def check(name, cond):
    res.append(cond)
    print(("PASS" if cond else "FAIL"), name)


R = "acme-widgets"
store.create_task("w-1", R, "b", "/tmp/a", intent="add sprocket cache")
store.set_state("w-1", "running")
store.create_task("w-2", R, "b", "/tmp/b", intent="fix gear")
store.set_state("w-2", "pr_ready")
store.set_pr_url("w-2", "https://example.invalid/acme/widgets/pull/7")
store.set_greenlight_tier("w-2", "GREEN")
store.create_task("w-3", R, "b", "/tmp/c", intent="tune cog")
store.set_state("w-3", "needs_human", "orphaned-on-restart")
store.create_task("o-1", "other-repo", "b", "/tmp/d", intent="unrelated")
store.record_merge(R, True, task_id="w-2")
store.append_event("w-2", "deployed", {})

p = status.status_path(R)
check("written on state change, under COXD_HOME/status", p.exists() and p.parent.name == "status")
txt = p.read_text()
check("open lists running", "## Open (1)" in txt and "w-1 [running] add sprocket cache" in txt)
check("ready lists PR", "## Ready to merge (1)" in txt and "pull/7" in txt)
check("stuck has next step", "## Stuck (1)" in txt and "w-3" in txt and "next:" in txt)
check("deploy recorded", "Last deploy: never" not in txt)
check("merge level + streak", "Merge level: GREEN" in txt and "Clean streak: 1" in txt)
check("other repo excluded", "o-1" not in txt)
check("path traversal sanitized", status.status_path("../x/y").parent == p.parent)
check("record_merge still returns streak", store.record_merge(R, True, task_id="w-2") == 1)
check("cli status prints", cli.main(["status", "--repo", R]) == 0)
sys.exit(0 if all(res) else 1)

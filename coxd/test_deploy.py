"""Unit test for landed._maybe_deploy — auto-deploy-after-merge decision logic.

Standalone (coxd has no pytest harness). Throwaway home:
    cd coxd && COXD_HOME=$(mktemp -d) .venv/bin/python test_deploy.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("COXD_HOME", tempfile.mkdtemp(prefix="coxd-dep-"))

import landed  # noqa: E402
import registry  # noqa: E402
import store  # noqa: E402


class FakeProc:
    def __init__(self, rc: int = 0, out: str = "") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, ""


ci = {"status": "completed", "conclusion": "success"}
deploy_rc = 0
deploy_ran: list = []


def fake_run(args, cwd=None):
    if args[:3] == ["gh", "run", "list"]:
        return FakeProc(0, json.dumps([ci]))
    if args and args[0] == "bash":          # the deploy command
        deploy_ran.append(cwd)
        return FakeProc(deploy_rc)
    return FakeProc(0)


landed._run = fake_run  # type: ignore[assignment]


def mk(tid: str) -> dict:
    store.create_task(tid, "aura-tutor", "brief", "/wt", repo_path="/repo/aura-tutor")
    store.set_pr_url(tid, "https://github.com/o/aura-tutor/pull/1")
    store.set_state(tid, "landed", "merged")
    return store.get_task(tid)


def kinds(tid: str) -> list:
    return [e["kind"] for e in store.events(tid)]


def set_deploy(cfg: dict) -> None:
    registry.save("aura-tutor", {"deploy": cfg})


res = []
def check(name, cond): res.append(cond); print(("PASS" if cond else "FAIL"), name)


# 1. disabled → no deploy
set_deploy({"enabled": False, "command": "./deploy-to-nas.sh"})
deploy_ran.clear(); landed._maybe_deploy(mk("t1"), "o/aura-tutor")
check("disabled → no deploy", not deploy_ran and "deploy-start" not in kinds("t1"))

# 2. enabled + CI red → skip
ci = {"status": "completed", "conclusion": "failure"}
set_deploy({"enabled": True, "command": "./deploy-to-nas.sh", "gate_on_ci": True, "branch": "main"})
deploy_ran.clear(); landed._maybe_deploy(mk("t2"), "o/aura-tutor")
check("CI red → skipped", not deploy_ran and "deploy-skipped" in kinds("t2"))

# 3. enabled + CI ok → deploy
ci = {"status": "completed", "conclusion": "success"}
landed._deploy_stamp("aura-tutor").unlink(missing_ok=True)
deploy_ran.clear(); landed._maybe_deploy(mk("t3"), "o/aura-tutor")
check("CI ok → deployed", bool(deploy_ran) and "deployed" in kinds("t3"))

# 4. recently deployed (t3 just stamped) → coalesce
deploy_ran.clear(); landed._maybe_deploy(mk("t4"), "o/aura-tutor")
check("recent deploy → coalesced", not deploy_ran and "deploy-coalesced" in kinds("t4"))

print("\nALL PASS ✓" if all(res) else "\nSOME FAILED ✗")
sys.exit(0 if all(res) else 1)

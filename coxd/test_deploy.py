"""Unit test for landed._maybe_deploy — auto-deploy-after-merge decision logic.

Standalone (coxd has no pytest harness). Throwaway home:
    cd coxd && COXD_HOME=$(mktemp -d) .venv/bin/python test_deploy.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time as _t

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-dep-")

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
    return FakeProc(0)


def fake_stream(tid, repo, timeout_s):  # the deployer call
    deploy_ran.append(repo)
    return deploy_rc, ""


landed._run = fake_run  # type: ignore[assignment]
real_stream = landed._stream_deploy
landed._stream_deploy = fake_stream  # type: ignore[assignment]


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
def check(name, cond):
    res.append(cond)
    print(("PASS" if cond else "FAIL"), name)


# 1. disabled → no deploy
set_deploy({"enabled": False, "command": "./deploy-to-nas.sh"})
deploy_ran.clear()
landed.run_deploy(mk("t1"), "o/aura-tutor")
check("disabled → no deploy", not deploy_ran and "deploy-start" not in kinds("t1"))

# 2. enabled + CI red → skip
ci = {"status": "completed", "conclusion": "failure"}
set_deploy({"enabled": True, "command": "./deploy-to-nas.sh", "gate_on_ci": True, "branch": "main"})
deploy_ran.clear()
landed.run_deploy(mk("t2"), "o/aura-tutor")
check("CI red → skipped", not deploy_ran and "deploy-skipped" in kinds("t2"))

# 3. enabled + CI ok → deploy
ci = {"status": "completed", "conclusion": "success"}
landed._deploy_stamp("aura-tutor").unlink(missing_ok=True)
deploy_ran.clear()
landed.run_deploy(mk("t3"), "o/aura-tutor")
check("CI ok → deployed", bool(deploy_ran) and "deployed" in kinds("t3"))

# 4. recently deployed (t3 just stamped) → coalesce
deploy_ran.clear()
landed.run_deploy(mk("t4"), "o/aura-tutor")
check("recent deploy → coalesced", not deploy_ran and "deploy-coalesced" in kinds("t4"))

# 4b. post-merge hook never deploys: held event + ping, deployer untouched
pings0: list = []
landed.notify.notify_async = (  # type: ignore[assignment]
    lambda title, body, prio="default": pings0.append(title))
deploy_ran.clear()
landed._deploy_stamp("aura-tutor").unlink(missing_ok=True)
landed._maybe_deploy(mk("t4b"), "o/aura-tutor")
check("post-merge → held, not deployed",
      not deploy_ran and "deploy-held" in kinds("t4b") and "deploy-start" not in kinds("t4b")
      and pings0 == ["coxd: deploy held"])

# 4c. manual deploy without COXD_DEPLOYER_URL → clear error, nothing runs
os.environ.pop("COXD_DEPLOYER_URL", None)
deploy_ran.clear()
r = landed.run_deploy(mk("t4c"), "o/aura-tutor", manual=True)
check("manual w/o deployer URL → error",
      "not configured" in r.get("error", "") and not deploy_ran)

# 5/6. real client against a fake deployer (the real deployer server, harmless commands)
import threading  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, "..", "deploy", "deployer"))
import server as deployer_server  # noqa: E402

root = tempfile.mkdtemp(prefix="fake-src-")
allow = {
    "okrepo": {"cmd": ["sh", "-c", "echo building; echo done"], "cwd": root},
    "badrepo": {"cmd": ["sh", "-c", "echo oops; exit 3"], "cwd": root},
    "slowrepo": {"cmd": ["sh", "-c", "sleep 20"], "cwd": root},
}
dep = deployer_server.Deployer(allow, "tok-test")
srv = ThreadingHTTPServer(("127.0.0.1", 0), deployer_server.make_handler(dep))
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["COXD_DEPLOYER_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
os.environ["COXD_DEPLOYER_TOKEN"] = "tok-test"
landed._PROGRESS_EVERY_S = 0.0
landed._stream_deploy = real_stream  # type: ignore[assignment]
pings: list = []
landed.notify.notify_async = (  # type: ignore[assignment]
    lambda title, body, prio="default": pings.append((title, prio)))
landed._latest_ci_failing = lambda slug, branch: False  # type: ignore[assignment]
landed._run = lambda args, cwd=None: FakeProc(0)  # type: ignore[assignment]

def mk_repo(tid: str, repo: str) -> dict:
    store.create_task(tid, repo, "brief", "/wt", repo_path=f"/repo/{repo}")
    store.set_pr_url(tid, f"https://github.com/o/{repo}/pull/1")
    store.set_state(tid, "landed", "merged")
    return store.get_task(tid)

for repo in ("okrepo", "badrepo"):
    registry.save(repo, {"deploy": {"enabled": True, "command": "x", "gate_on_ci": False}})

pings.clear()
res_ok = landed.run_deploy(mk_repo("t5", "okrepo"), "o/okrepo", manual=True)
check("fake deployer success → deployed event",
      res_ok.get("ok") and "deployed" in kinds("t5") and "deploy-failed" not in kinds("t5"))
check("success relays log as progress", "deploy-progress" in kinds("t5"))

pings.clear()
res_bad = landed.run_deploy(mk_repo("t5b", "badrepo"), "o/badrepo", manual=True)
ev = [e for e in store.events("t5b") if e["kind"] == "deploy-failed"]
check("fake deployer failure → deploy-failed event + ping",
      "error" in res_bad and len(ev) == 1 and "oops" in json.dumps(ev[0]) and
      any(p[1] == "high" for p in pings))

# 6. deadline → rc 124; unknown repo / bad token → rc 127
rc, out = landed._stream_deploy("t6", "slowrepo", 1)
check("stream: timeout rc 124", rc == 124 and "timed out" in out)
rc, out = landed._stream_deploy("t6", "nope", 5)
check("unknown repo → rc 127", rc == 127 and "allowlist" in out)
os.environ["COXD_DEPLOYER_TOKEN"] = "wrong"
rc, out = landed._stream_deploy("t6", "okrepo", 5)
check("bad token → rc 127", rc == 127)
os.environ["COXD_DEPLOYER_TOKEN"] = "tok-test"

# 7. a crash inside the background thread still lands a terminal deploy-failed event
def boom(*a, **k):
    raise RuntimeError("kaboom")
landed.ci_triage.repo_slug = lambda path: "o/aura-tutor"  # type: ignore[assignment]
orig = landed.run_deploy
landed.run_deploy = boom  # type: ignore[assignment]
mk("t7")
landed.deploy_task_async("t7")
for _ in range(50):
    if "deploy-failed" in kinds("t7"):
        break
    _t.sleep(0.05)
landed.run_deploy = orig  # type: ignore[assignment]
check("bg crash → deploy-failed event", "deploy-failed" in kinds("t7"))

print("\nALL PASS ✓" if all(res) else "\nSOME FAILED ✗")
sys.exit(0 if all(res) else 1)

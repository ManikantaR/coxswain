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


SHA = "a" * 40
streamed: list = []


def fake_stream(tid, repo, timeout_s, sha, on_start=None):  # the deployer call
    deploy_ran.append(repo)
    streamed.append((repo, sha, timeout_s))
    if on_start:
        on_start()
    return deploy_rc, ""


landed._run = fake_run  # type: ignore[assignment]
real_stream = landed._stream_deploy
landed._stream_deploy = fake_stream  # type: ignore[assignment]
landed._branch_head = lambda slug, branch: SHA  # type: ignore[assignment]


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
check("recent deploy of same sha → coalesced",
      not deploy_ran and "deploy-coalesced" in kinds("t4"))
landed.run_deploy(mk("t4a"), "o/aura-tutor", sha="b" * 40)
check("different sha is NOT coalesced", deploy_ran == ["aura-tutor"] and streamed[-1][1] == "b" * 40)

# 4b. post-merge hook: deploys the merged commit unless the PR touches migrations (#53)
pings0: list = []
landed.notify.notify_async = (  # type: ignore[assignment]
    lambda title, body, prio="default": pings0.append(title))
os.environ["COXD_DEPLOYER_URL"] = "http://unused.invalid:1"
merge_sha, touches = "c" * 40, False
landed._pr_merge_sha = lambda url, slug: merge_sha  # type: ignore[assignment]
landed._touches_migrations = lambda url, slug: touches  # type: ignore[assignment]
_real_bg = landed._deploy_bg
landed._deploy_bg = lambda t, slug, manual, sha=None: (  # type: ignore[assignment]
    landed.run_deploy(t, slug, manual=manual, sha=sha) or True)
deploy_ran.clear()
landed._deploy_stamp("aura-tutor").unlink(missing_ok=True)
landed._maybe_deploy(mk("t4b"), "o/aura-tutor")
check("post-merge → deploys the merge commit",
      deploy_ran == ["aura-tutor"] and streamed[-1][1] == merge_sha
      and "deployed" in kinds("t4b") and "deploy-held" not in kinds("t4b"))
touches = True
deploy_ran.clear()
landed._maybe_deploy(mk("t4e"), "o/aura-tutor")
check("post-merge with migrations → held, not deployed",
      not deploy_ran and "deploy-held" in kinds("t4e") and "deploy-start" not in kinds("t4e")
      and "coxd: deploy held" in pings0)
touches = None
landed._maybe_deploy(mk("t4f"), "o/aura-tutor")
check("post-merge, migrations unknown → held", not deploy_ran and "deploy-held" in kinds("t4f"))
touches = False
merge_sha = None
landed._maybe_deploy(mk("t4g"), "o/aura-tutor")
check("post-merge, no merge sha → held", not deploy_ran and "deploy-held" in kinds("t4g"))
os.environ.pop("COXD_DEPLOYER_URL")
landed._deploy_bg = _real_bg  # type: ignore[assignment]

# 4c. manual deploy without COXD_DEPLOYER_URL → clear error, nothing runs
os.environ.pop("COXD_DEPLOYER_URL", None)
deploy_ran.clear()
r = landed.run_deploy(mk("t4c"), "o/aura-tutor", manual=True)
check("manual w/o deployer URL → error",
      "not configured" in r.get("error", "") and not deploy_ran)

# 4d. board path: sync error when the URL is unset (no silent background no-op)
landed.ci_triage.repo_slug = lambda path: "o/aura-tutor"  # type: ignore[assignment]
r = landed.deploy_task_async(mk("t4d")["id"])
check("deploy_task_async w/o URL → sync error", "not configured" in r.get("error", ""))

# 5/6. real client against a fake deployer (the real deployer server, harmless commands)
import threading  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, "..", "deploy", "deployer"))
import server as deployer_server  # noqa: E402

root = tempfile.mkdtemp(prefix="fake-src-")
import subprocess  # noqa: E402
_git = ["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
subprocess.run(_git + ["init", "-q"], check=True)
subprocess.run(_git + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
HEAD = subprocess.run(_git + ["rev-parse", "HEAD"], capture_output=True, text=True,
                      check=True).stdout.strip()
landed._branch_head = lambda slug, branch: HEAD  # type: ignore[assignment]
allow = {
    "okrepo": {"cmd": ["sh", "-c", "echo building; echo done"], "cwd": root},
    "badrepo": {"cmd": ["sh", "-c", "echo oops; exit 3"], "cwd": root},
    "stalerepo": {"cmd": ["sh", "-c", "echo synced nothing"], "cwd": root},
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

for repo in ("okrepo", "badrepo", "stalerepo"):
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

# stale source: command exits 0 but HEAD != requested sha → deploy-failed(stale-source)
pings.clear()
res_stale = landed.run_deploy(mk_repo("t5c", "stalerepo"), "o/stalerepo", manual=True,
                              sha="d" * 40)
ev = [e for e in store.events("t5c") if e["kind"] == "deploy-failed"]
check("stale source → deploy-failed stale-source, never 'deployed'",
      "error" in res_stale and len(ev) == 1 and "stale-source" in json.dumps(ev[0])
      and "deployed" not in kinds("t5c"))
rc, out = landed._stream_deploy("t6", "okrepo", 5, "not-a-sha")
check("bad sha refused client-side", rc == 127 and "bad commit sha" in out)

# 409: busy deployer → deploy-skipped, no stamp written
landed._deploy_stamp("stalerepo").unlink(missing_ok=True)
registry.save("stalerepo", {"deploy": {"enabled": True, "command": "x", "gate_on_ci": False}})
_orig_req = landed._deployer_req
def _busy(method, path, timeout=30.0):
    raise landed.DeployerError("deployer refused: busy", 409)
landed._deployer_req = _busy  # type: ignore[assignment]
r409 = landed.run_deploy(mk_repo("t5d", "stalerepo"), "o/stalerepo", manual=True)
landed._deployer_req = _orig_req  # type: ignore[assignment]
check("409 → deploy-skipped, no coalesce stamp",
      "error" in r409 and "deploy-skipped" in kinds("t5d")
      and not landed._deploy_stamp("stalerepo").exists())

# 6. deadline → rc 124; unknown repo / bad token → rc 127
rc, out = landed._stream_deploy("t6", "slowrepo", 1, HEAD)
check("stream: timeout rc 124", rc == 124 and "timed out" in out)
rc, out = landed._stream_deploy("t6", "nope", 5, HEAD)
check("unknown repo → rc 127", rc == 127 and "allowlist" in out)
os.environ["COXD_DEPLOYER_TOKEN"] = "wrong"
rc, out = landed._stream_deploy("t6", "okrepo", 5, HEAD)
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

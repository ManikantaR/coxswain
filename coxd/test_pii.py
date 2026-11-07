"""PII/secret gate tests — invented data only; fake secrets built by concatenation.

Standalone:  cd coxd && ../.venv/bin/python test_pii.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-pii-")

import gate  # noqa: E402
import pii  # noqa: E402
import sandbox  # noqa: E402
import ship  # noqa: E402
import store  # noqa: E402

fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


def rules(text: str, deny: list[str] | None = None) -> set[str]:
    return {h.rule for h in pii.scan({"loc": text}, deny or [])}


GH = "ghp" + "_" + "a1B2" * 9
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
PK = "-----BEGIN RSA PRIVATE" + " KEY-----"
ANT = "sk-ant-" + "api03-" + "x" * 30
for n, v in [("github", GH), ("aws", AWS), ("privkey", PK), ("anthropic", ANT)]:
    check(f"secret fires: {n}", rules(f"x = '{v}'") == {"secret"})
check("ssn fires", rules("id 900-12-3456 here") == {"ssn"})
LUHN = "4929" + "0000" + "0000" + "0000"  # invented, Luhn-valid
check("luhn-valid card fires", pii._luhn(LUHN) and rules(f"n={LUHN}") == {"card"})
check("published test card ok", rules("4242" + "4242" + "4242" + "4242") == set())
check("non-Luhn run ok", rules("1234567890123456") == set())
check("denylist whole word, any case", rules("hello ZORBLAX!", ["zorblax"]) == {"denylist"})
check("denylist not inside longer word", rules("zorblaxian", ["zorblax"]) == set())

secret_text = "ssn 900-12-3456 " + GH
hits = pii.scan({"f": secret_text}, [])
check("Hit never holds matched text",
      all(secret_text not in repr(h) and "900-12-3456" not in repr(h) and GH not in repr(h)
          for h in hits) and hits)

# --- diff parsing: only added lines
diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n"
        "-ssn 900-12-3456\n+clean\n")
check("removed line not scanned", rules(ship._added_lines(diff)["a.py"]) == set())

# --- ship() integration with stubbed git/gh
store.create_task("t1", "repo", "brief", "/wt", repo_path="/repo/x")
calls: list[list[str]] = []
state = {"diff": "", "log": "msg\0", "denylist": None, "fail": False}


def fake_wt(cmd, wt, *a, **k):
    if state["fail"]:
        raise RuntimeError("boom")
    out = state["diff"] if cmd[1] == "diff" else state["log"] if cmd[1] == "log" else ""
    return SimpleNamespace(returncode=0, stdout=out, stderr="")


def fake_run(cmd, *a, **k):
    calls.append(list(cmd))
    if cmd[:3] == ["git", "remote", "get-url"]:
        return SimpleNamespace(returncode=0, stdout="git@github.com:o/r", stderr="")
    return SimpleNamespace(returncode=0, stdout="https://github.com/o/r/pull/1", stderr="")


sandbox.run_in_worktree = fake_wt
gate._base_ref = lambda wt: "origin/main"
ship.subprocess.run = fake_run
ship._greenlight = lambda t, wt: ("", "")
ship._build_body = lambda t, wt, h="": "body"


def pushed() -> bool:
    return any(c[:2] == ["git", "push"] or c[:3] == ["gh", "pr", "create"] for c in calls)


def events(tid: str) -> list:
    return store.events(tid)


state["diff"] = "+++ b/f.py\n+tok = '" + GH + "'\n"
out, _ = ship.ship("t1")
t = store.get_task("t1")
check("hit: no push/pr, pii-block", out == "pii-block" and not pushed()
      and t["state"] == "needs_human" and t["reason"] == "pii-block")
ev = json.dumps(events("t1"), default=str)
check("event has no matched text, has location", GH not in ev and "diff:f.py" in ev)

state["diff"] = ""
calls.clear()
out, _ = ship.ship("t1")
check("missing denylist: clean ship proceeds", out == "pr" and pushed())
check("denylist-missing event emitted", "pii-denylist-missing" in
      json.dumps(events("t1"), default=str))

state["diff"] = "+++ b/f.py\n+ssn 900-12-3456\n"  # pattern rules still run, no denylist
calls.clear()
out, _ = ship.ship("t1")
check("missing denylist still runs patterns", out == "pii-block" and not pushed())

Path(os.environ["COXD_HOME"], "pii-denylist.txt").write_text("# c\nzorblax\n")
state["diff"] = "+++ b/f.py\n+hi Zorblax\n"
out, _ = ship.ship("t1")
check("denylist file loaded fresh", out == "pii-block")

state["diff"] = ""
state["log"] = "fix 900-12-3456\0"
out, _ = ship.ship("t1")
check("commit message scanned", out == "pii-block")
state["log"] = "msg\0"

state["fail"] = True
calls.clear()
out, _ = ship.ship("t1")
t = store.get_task("t1")
check("scan error blocks", out == "pii-scan-error" and not pushed()
      and t["reason"] == "pii-scan-error")

print("FAILED" if fails else "all ok")
sys.exit(1 if fails else 0)

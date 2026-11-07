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

import subprocess  # noqa: E402

import pii  # noqa: E402
import ship  # noqa: E402
import store  # noqa: E402

fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


def rules(text: str, deny: list[str] | None = None) -> set[str]:
    return {h.rule for h in pii.scan({"loc": text}, deny or [])}


SSN = "900" + "-" + "12" + "-" + "3456"  # invented
GH = "ghp" + "_" + "a1B2" * 9
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
PK = "-----BEGIN RSA PRIVATE" + " KEY-----"
ANT = "sk-ant-" + "api03-" + "x" * 30
for n, v in [("github", GH), ("aws", AWS), ("privkey", PK), ("anthropic", ANT)]:
    check(f"secret fires: {n}", rules(f"x = '{v}'") == {"secret"})
check("ssn fires", rules(f"id {SSN} here") == {"ssn"})
LUHN = "4929" + "0000" + "0000" + "0000"  # invented, Luhn-valid
check("luhn-valid card fires", pii._luhn(LUHN) and rules(f"n={LUHN}") == {"card"})
check("published test card ok", rules("4242" + "4242" + "4242" + "4242") == set())
check("non-Luhn run ok", rules("1234567890123456") == set())
check("denylist whole word, any case", rules("hello ZORBLAX!", ["zorblax"]) == {"denylist"})
check("denylist not inside longer word", rules("zorblaxian", ["zorblax"]) == set())

secret_text = f"ssn {SSN} " + GH
hits = pii.scan({"f": secret_text}, [])
check("Hit never holds matched text",
      all(secret_text not in repr(h) and SSN not in repr(h) and GH not in repr(h)
          for h in hits) and hits)

# --- diff parsing: only added lines; hunk-aware
diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n"
        f"-ssn {SSN}\n+clean\n")
check("removed line not scanned", rules(ship._added_lines(diff)["a.py"]) == set())
# an added line that looks like a file header / dev-null header is still scanned
evil = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,3 @@\n"
        f"+++ {GH}\n+++ /dev/null\n+ssn {SSN}\n")
got = ship._added_lines(evil)["a.py"]
check("header-lookalike added lines scanned", GH in got and SSN in got)
check("unicode line separators don't truncate",
      SSN in ship._added_lines("+++ b/a\n@@ -0,0 +1 @@\n+x\u2028" + SSN + "\n")["a"])

# --- real git: add-then-remove history, binary-by-attributes, non-UTF-8
tmp = Path(tempfile.mkdtemp(prefix="coxd-pii-git-"))
GENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.test",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.test"}


def g(*a: str) -> None:
    subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True, env=GENV)


g("init", "-q", "-b", "main")
(tmp / "a.txt").write_text("base\n")
g("add", ".")
g("commit", "-qm", "base")
g("update-ref", "refs/remotes/origin/main", "HEAD")
g("checkout", "-qb", "coxd/x")
(tmp / "a.txt").write_text("base\nkey=" + GH + "\n")
(tmp / ".gitattributes").write_text("b.env -diff\n")
(tmp / "b.env").write_text("s=" + AWS + "\n")
(tmp / "l1.txt").write_bytes(b"caf\xe9 " + SSN.encode() + b"\n")
g("add", ".")
g("commit", "-qm", "add")
(tmp / "a.txt").write_text("base\n")
(tmp / "b.env").unlink()
g("rm", "-q", "--cached", "b.env", "--ignore-unmatch")
g("add", "-A")
g("commit", "-qm", "remove")
items = ship._pii_items(tmp, "origin/main", "refs/heads/coxd/x", "t", "b")
found = {(h.rule, h.location) for h in pii.scan(items, [])}
check("add-then-remove history is caught", ("secret", "diff:a.txt") in found)
check("-diff attribute file still scanned", ("secret", "diff:b.env") in found)
check("non-UTF-8 text scanned, no crash", ("ssn", "diff:l1.txt") in found)
try:
    ship._pii_items(tmp, "origin/nope", "refs/heads/coxd/x", "t", "b")
    check("unresolvable base raises (fail closed)", False)
except Exception:
    check("unresolvable base raises (fail closed)", True)

# --- ship() integration with stubbed git/gh
store.create_task("t1", "repo", "brief", "/wt", repo_path="/repo/x")
calls: list[list[str]] = []
state = {"diff": "", "log": "msg", "fail": False}


def fake_items(repo_path, base, ref, title, body):
    if state["fail"]:
        raise RuntimeError("boom")
    items = {f"diff:{p}": t for p, t in ship._added_lines(state["diff"]).items()}
    items["commit:0"] = state["log"]
    items["pr-title"], items["pr-body"] = title, body
    return items


def fake_run(cmd, *a, **k):
    calls.append(list(cmd))
    if cmd[:3] == ["git", "remote", "get-url"]:
        return SimpleNamespace(returncode=0, stdout="git@github.com:o/r", stderr="")
    return SimpleNamespace(returncode=0, stdout="https://github.com/o/r/pull/1", stderr="")


def hunk(path: str, line: str) -> str:
    return f"diff --git a/{path} b/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n+{line}\n"


ship._pii_items = fake_items
ship.subprocess.run = fake_run
ship._greenlight = lambda t, wt: ("", "")
ship._build_body = lambda t, wt, h="": "body"


def pushed() -> bool:
    return any(c[:2] == ["git", "push"] or c[:3] == ["gh", "pr", "create"] for c in calls)


def evs(tid: str) -> str:
    return json.dumps(store.events(tid), default=str)


state["diff"] = hunk("f.py", "tok = '" + GH + "'")
out, _ = ship.ship("t1")
check("hit: no push/pr, pii-block", out == "pii-block" and not pushed())
ev = evs("t1")
check("event has no matched text, has location", GH not in ev and "diff:f.py" in ev)

state["diff"] = ""
calls.clear()
out, _ = ship.ship("t1")
check("missing denylist: clean ship proceeds", out == "pr" and pushed())
check("denylist-missing event emitted", "pii-denylist-missing" in evs("t1"))

state["diff"] = hunk("f.py", "ssn " + SSN)  # pattern rules still run, no denylist
calls.clear()
out, _ = ship.ship("t1")
check("missing denylist still runs patterns", out == "pii-block" and not pushed())

Path(os.environ["COXD_HOME"], "pii-denylist.txt").write_text("# c\nzorblax\n")
state["diff"] = hunk("f.py", "hi Zorblax")
out, _ = ship.ship("t1")
check("denylist file loaded fresh", out == "pii-block")

state["diff"] = ""
state["log"] = "fix " + SSN
out, _ = ship.ship("t1")
check("commit message scanned", out == "pii-block")
state["log"] = "msg"

state["fail"] = True
calls.clear()
out, _ = ship.ship("t1")
check("scan error blocks", out == "pii-scan-error" and not pushed())
state["fail"] = False

ship._build_body = lambda t, wt, h="": 1 / 0
calls.clear()
out, _ = ship.ship("t1")
check("body-build error blocks as pii-scan-error", out == "pii-scan-error" and not pushed())

print("FAILED" if fails else "all ok")
sys.exit(1 if fails else 0)

"""Label intake with `gh` mocked (invented data only).

    cd coxd && ../.venv/bin/python test_intake.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-intake-")

import ci_triage  # noqa: E402
import dispatch  # noqa: E402
import factory  # noqa: E402
import intake  # noqa: E402
import pii  # noqa: E402
import registry  # noqa: E402
import repos  # noqa: E402
import store  # noqa: E402

fails = 0
open(pii.denylist_path(), "w").write("Zebulon Quux\n")


def check(name, cond):
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


SLUG = "acme/widgets"
URL = f"https://github.com/{SLUG}/issues/"
calls: list[list[str]] = []
issues: dict[int, dict] = {}
actors: dict[int, dict] = {}
perms: dict[str, str] = {"bob": "write", "alice": "write", "rita": "read"}
dispatched: list[str] = []
edits: dict[int, tuple[str, str]] = {}
renames: dict[int, str] = {}


def fake_gh(args):
    calls.append(args)
    a = args
    if a[0] == "label" and a[1] == "list":
        return 0, json.dumps([{"name": "coxd-ready"}])
    if a[0] == "label":
        return 0, ""
    if a[0] == "issue" and a[1] == "list":
        return 0, json.dumps([i for i in issues.values()
                              if "coxd-ready" in [lb["name"] for lb in i["labels"]]
                              and i["state"] == "OPEN"])
    if a[0] == "issue" and a[1] == "view":
        i = issues[int(a[2])]
        return 0, json.dumps({"state": i["state"], "labels": i["labels"]})
    if a[0] == "issue" and a[1] == "edit":
        i = issues[int(a[2])]
        names = [lb["name"] for lb in i["labels"]]
        for k, v in enumerate(a):
            if v == "--remove-label":
                names = [x for x in names if x != a[k + 1]]
            if v == "--add-label":
                names.append(a[k + 1])
        i["labels"] = [{"name": n} for n in names]
        return 0, ""
    if a[0] == "api" and a[1].endswith("/timeline"):
        n = int(a[1].split("/")[4])
        evs = [{"event": "labeled", "label": {"name": "coxd-ready"},
                "created_at": "2026-03-01T00:00:00Z", "actor": actors[n]}]
        if n in renames:
            evs.append({"event": "renamed", "created_at": "2026-03-02T00:00:00Z",
                        "actor": {"login": renames[n], "type": "User"}})
        return 0, json.dumps([evs])
    if a[0] == "api" and a[1] == "graphql":
        n = int(a[-1].split("=")[-1])
        e = edits.get(n)
        node = {"lastEditedAt": e[0], "userContentEdits": {"nodes": [
            {"editedAt": e[0], "editor": {"login": e[1], "__typename": "User"}}]}} if e else {}
        return 0, json.dumps({"data": {"repository": {"issue": node}}})
    if a[0] == "api" and "/collaborators/" in a[1]:
        who = a[1].split("/")[4]
        return 0, json.dumps({"permission": perms.get(who, "none")})
    raise AssertionError(args)


def fake_dispatch(path, brief):
    dispatched.append(brief)
    tid = f"widgets-t{len(dispatched)}"
    store.create_task(tid, "widgets", brief, "/tmp/x", repo_path=path)
    return tid


def add(n, body="Do a thing.", labels=("coxd-ready",), who="alice", typ="User",
        created=None, title="Add a thing"):
    issues[n] = {"number": n, "title": title, "body": body, "url": URL + str(n),
                 "createdAt": created or f"2026-01-0{n}T00:00:00Z", "state": "OPEN",
                 "labels": [{"name": x} for x in labels]}
    actors[n] = {"login": who, "type": typ}


def names(n):
    return sorted(lb["name"] for lb in issues[n]["labels"])


intake._gh = fake_gh
ci_triage.repo_slug = lambda p: SLUG
dispatch.dispatch = fake_dispatch
repos.list_repos = lambda: [{"name": "widgets", "path": "/tmp/widgets"}]
registry.save("widgets", {"intake": True})

# trusted label: dispatched once, labels swap, second poll no-op
add(1)
check("poll takes 1", intake.poll_once() == 1)
check("dispatched once w/ issue url", len(dispatched) == 1 and URL + "1" in dispatched[0])
check("labels swapped", names(1) == ["coxd-working"])
check("board status", intake.status()["taken"] == 1 and intake.status()["at"])
# even if the label is put back, the existing task blocks a duplicate
issues[1]["labels"] = [{"name": "coxd-ready"}]
check("second poll no dispatch", intake.poll_once() == 0 and len(dispatched) == 1)
issues[1]["state"] = "CLOSED"

# untrusted: read-only user, bot (type and login suffix)
add(2, who="rita")
add(3, who="alice[bot]", typ="Bot")
add(4, who="alice[bot]", typ="User")
n0 = len(dispatched)
check("untrusted none taken", intake.poll_once() == 0 and len(dispatched) == n0)
check("untrusted labelled", all(names(i) == ["coxd-untrusted"] for i in (2, 3, 4)))
for i in (2, 3, 4):
    issues[i]["state"] = "CLOSED"

# dependency
add(5, body="Depends on #6\nmore")
add(6, labels=())
check("dep holds", intake.poll_once() == 0 and names(5) == ["coxd-ready", "coxd-waiting"])
intake.poll_once()
check("waiting added once", names(5).count("coxd-waiting") == 1)
issues[6]["state"] = "CLOSED"
check("dep closed → dispatched", intake.poll_once() == 1 and names(5) == ["coxd-working"])
issues[5]["state"] = "CLOSED"

# zero-width stripped
add(7, body="Fix\u200b the\u202e bug\ufeff", title="Ti\u200btle")
intake.poll_once()
check("zero-width stripped", "\u200b" not in dispatched[-1] and "\u202e" not in dispatched[-1]
      and "\ufeff" not in dispatched[-1] and "Fix the bug" in dispatched[-1])
issues[7]["state"] = "CLOSED"

# PII hit
open(pii.denylist_path(), "w").write("Zebulon Quux\n")
add(8, body="Contact zebulon quux about it")
add(9, body="zebu\u200blon quux sneaky")
n0, c0 = len(dispatched), len(calls)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    taken = intake.poll_once()
log = buf.getvalue()
check("pii not dispatched", taken == 0 and len(dispatched) == n0)
check("pii labelled", names(8) == ["coxd-pii-block"] and names(9) == ["coxd-pii-block"])
check("no comment", not any(c[:2] in (["issue", "comment"], ["pr", "comment"]) for c in calls))
check("no matched text in log/calls", "quux" not in (log + json.dumps(calls[c0:])).lower())
check("rule+location logged", "denylist@body" in log)
check("no events leak", all("quux" not in json.dumps(store.events(t["id"])).lower()
                            for t in store.list_tasks()))
for i in (8, 9):
    issues[i]["state"] = "CLOSED"

# max 3 per repo
for i in range(10, 15):
    add(i, created=f"2026-02-{i:02d}T00:00:00Z")
check("max 3 per poll", intake.poll_once() == 3)
for i in range(10, 15):
    issues[i]["state"] = "CLOSED"

# edited after labelling by an untrusted user -> blocked; by a trusted one -> ok
add(30, who="alice")
add(31, who="alice")
edits[30] = ("2026-03-02T00:00:00Z", "rita")
edits[31] = ("2026-03-02T00:00:00Z", "alice")
n0 = len(dispatched)
check("trusted edit ok, untrusted edit blocked",
      intake.poll_once() == 1 and len(dispatched) == n0 + 1
      and names(30) == ["coxd-untrusted"] and names(31) == ["coxd-working"])
edits.clear()
for i in (30, 31):
    issues[i]["state"] = "CLOSED"

# rename after labelling: untrusted renamer blocked, trusted renamer ok
add(32)
add(33)
renames[32] = "rita"
renames[33] = "bob"
n0 = len(dispatched)
check("rename: untrusted blocked, trusted ok",
      intake.poll_once() == 1 and len(dispatched) == n0 + 1
      and names(32) == ["coxd-untrusted"] and names(33) == ["coxd-working"])
renames.clear()
for i in (32, 33):
    issues[i]["state"] = "CLOSED"

# stuck tasks: working -> needs-human; landed later clears it
add(40, labels=("coxd-working",))
add(41, labels=("coxd-working",))
store.create_task("widgets-stuck1", "widgets", f"x\n\nGitHub issue: {URL}40", "/tmp/x",
                  repo_path="/tmp/widgets")
store.create_task("widgets-stuck2", "widgets", f"x\n\nGitHub issue: {URL}41", "/tmp/x",
                  repo_path="/tmp/widgets")
store.set_state("widgets-stuck1", "needs_human", "gate-red")
store.set_state("widgets-stuck2", "failed")
intake.poll_once()
check("stuck labels swapped", names(40) == ["coxd-needs-human"]
      and names(41) == ["coxd-needs-human"])
c0 = len(calls)
intake.poll_once()
check("stuck sync idempotent", not any(c[:2] == ["issue", "edit"] for c in calls[c0:]))
check("no comment on stuck", not any(c[:2] == ["issue", "comment"] for c in calls))
intake.clear_working("widgets", SLUG, f"x\n\nGitHub issue: {URL}40")
check("landed clears needs-human", names(40) == [])

# paused / flag off → no calls
add(20)
c0 = len(calls)
factory.set_paused(True)
check("paused → None, no calls", intake.poll_once() is None and len(calls) == c0)
factory.set_paused(False)
registry.save("widgets", {"intake": False})
check("intake false → no calls", intake.poll_once() == 0 and len(calls) == c0)
registry.save("widgets", {})
check("flag absent → no calls", intake.poll_once() == 0 and len(calls) == c0)

# landed → coxd-working removed
registry.save("widgets", {"intake": True})
add(21, labels=("coxd-working",))
intake.clear_working("widgets", SLUG, f"x\n\nGitHub issue: {URL}21")
check("working cleared on land", names(21) == [])

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)

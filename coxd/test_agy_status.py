"""Live agy status on the board card: POST /api/task/{tid}/agy-status (display only).

    cd coxd && python test_agy_status.py
(invented data only)
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-agystat-")
os.environ.pop("COXD_OWNER_USER", None)
os.environ.pop("COXD_PROXY_SECRET", None)

import board  # noqa: E402
import store  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

c = TestClient(board.app, base_url="http://localhost:8791")
fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


def mk(tid, lane, state):
    store.create_task(tid, "widgets", "brief", "/tmp/x", lane=lane)
    store.set_state(tid, state)


mk("A1", "agy-handoff", "waiting_agy")
mk("A2", "agy-handoff", "working")
mk("C1", None, "working")
OK = {"step": 3, "action": "edit: web/x.ts", "elapsed_s": 125, "tokens": 4200}


def post(tid, body=None, **kw):
    return c.post(f"/api/task/{tid}/agy-status", json=OK if body is None else body, **kw)


def row(tid):
    return next(x for x in c.get("/api/tasks").json()["tasks"] if x["id"] == tid)


def n_events(tid):
    return sum(e["kind"] == "agy-status" for e in store.events(tid))


check("no status before any post", row("A1")["agy_status"] is None)
check("accepted for waiting_agy agy task", post("A1").status_code == 200)
st = row("A1")["agy_status"]
check("status shown on the card payload",
      st and st["action"] == "edit: web/x.ts" and st["step"] == 3
      and st["elapsed_s"] == 125 and st["tokens"] == 4200 and abs(st["ts"] - time.time()) < 5)
check("agy task in another state -> 400", post("A2").status_code == 400)
check("claude-lane task -> 404", post("C1").status_code == 404)
check("unknown task -> 404", post("nope").status_code == 404)
check("coxd-ready card unchanged",
      row("C1")["agy_status"] is None and row("C1")["lane"] == "claude")
check("no state transition", store.get_task("A1")["state"] == "waiting_agy")

# validation and caps
for name, body in {
    "missing field": {"step": 1, "action": "x", "elapsed_s": 1},
    "negative step": {**OK, "step": -1},
    "huge tokens": {**OK, "tokens": 10**12},
    "float": {**OK, "step": 1.5},
    "bool": {**OK, "step": True},
    "string int": {**OK, "tokens": "5"},
    "non-str action": {**OK, "action": 5},
    "empty action": {**OK, "action": "  "},
    "control chars": {**OK, "action": "a\x1b[31mb"},
    "newline": {**OK, "action": "a\nb"},
    "not an object": [1, 2],
}.items():
    check("rejects " + name, post("A1", body).status_code == 400)
check("rejects invalid JSON", c.post("/api/task/A1/agy-status", content="{nope",
      headers={"content-type": "application/json"}).status_code == 400)
check("rejects non-JSON content type", c.post(
    "/api/task/A1/agy-status", content="{}",
    headers={"content-type": "text/plain"}).status_code == 415)
check("rejects oversized body", post("A1", {**OK, "pad": "x" * 5000}).status_code == 413)
check("rejected posts did not clobber status", row("A1")["agy_status"]["step"] == 3)
check("action capped at 120", post("A1", {**OK, "action": "y" * 500}).status_code == 200
      and len(row("A1")["agy_status"]["action"]) == 120)

# guard: foreign origin refused like the other POSTs
check("foreign origin -> 403",
      post("A1", headers={"origin": "https://evil.example"}).status_code == 403)

# event rate limit: at most one agy-status event per 60s per task
check("one event so far (burst of posts)", n_events("A1") == 1)
board._AGY_EVENT_AT["A1"] -= 61
post("A1")
check("another event after 60s", n_events("A1") == 2)

# escaped render: the JS escapes the action before inserting it into the card
html = c.get("/").text
check("card line escapes action", "esc(a.action)" in html and "agy idle" in html
      and "updated '+age+'s ago" in html and "k tokens" in html)
XSS = "<img src=x onerror=1>"
check("raw markup survives only as data", post("A1", {**OK, "action": XSS}).status_code == 200
      and row("A1")["agy_status"]["action"] == XSS)

# stale (earlier round) status is not shown once the task re-enters waiting_agy
time.sleep(0.01)
store.set_state("A1", "needs_choice")
check("hidden outside waiting_agy", row("A1")["agy_status"] is None)
store.set_state("A1", "waiting_agy")
check("hidden for the next round until a new post", row("A1")["agy_status"] is None)
check("post again works", post("A1").status_code == 200 and row("A1")["agy_status"] is not None)

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)

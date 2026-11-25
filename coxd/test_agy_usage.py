"""agy token usage and estimated subscription cost on the board: POST /api/task/{tid}/agy-usage.

    cd coxd && python test_agy_usage.py
(invented data only)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-agyusage-")
os.environ.pop("COXD_OWNER_USER", None)
os.environ.pop("COXD_PROXY_SECRET", None)

import agyusage  # noqa: E402
import board  # noqa: E402
import registry  # noqa: E402
import stepspend  # noqa: E402
import store  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

c = TestClient(board.app, base_url="http://localhost:8791")
fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


def mk(tid: str, lane: str | None, state: str) -> None:
    store.create_task(tid, "widgets", "brief", "/tmp/x", lane=lane)
    store.set_state(tid, state)


mk("T1", "agy-handoff", "waiting_agy")
mk("T2", "agy-handoff", "working")
mk("C1", None, "working")

OK = {
    "input_tokens": 120_000,
    "output_tokens": 30_000,
    "thinking_tokens": 10_000,
    "cache_read_tokens": 60_000,
    "duration_seconds": 360,
    "model": "gemini-4-hydrogen-high",
}


def post(tid: str, body=None, **kw):
    return c.post(f"/api/task/{tid}/agy-usage", json=OK if body is None else body, **kw)


def payload(show_archived: bool = False) -> dict:
    return c.get("/api/tasks" + ("?all=1" if show_archived else "")).json()


def row(tid: str) -> dict:
    return next(x for x in payload()["tasks"] if x["id"] == tid)


def usage_events(tid: str) -> list[dict]:
    return [e for e in store.events(tid) if e["kind"] == "agy-usage"]


# --- 1. Estimate math & price override ---------------------------------------
# Hydrogen: 120k * 1.50/1e6 (0.18) + (30k+10k) * 7.50/1e6 (0.30) + 60k * 0.15/1e6 (0.009) = 0.489
check("estimate Hydrogen hand-computed", abs(agyusage.estimate(OK) - 0.489) < 1e-9)

unk = {
    "input_tokens": 1_000_000,
    "output_tokens": 200_000,
    "thinking_tokens": 100_000,
    "cache_read_tokens": 500_000,
    "duration_seconds": 60,
    "model": "test-model-1",
}
d_rates = agyusage.PRICES["default"]
want_unk = (
    1.0 * d_rates["input"]
    + 0.3 * d_rates["output"]
    + 0.5 * d_rates["cache_read"]
)
check("estimate unknown model uses default entry", abs(agyusage.estimate(unk) - want_unk) < 1e-9)

override_path = registry.home() / "agy-prices.json"
override_path.write_text(
    json.dumps({"test-model-1": {"input": 2.0, "output": 10.0, "cache_read": 0.2}}),
    encoding="utf-8",
)
# 1M * 2.0 + 0.3M * 10.0 + 0.5M * 0.2 = 2.0 + 3.0 + 0.1 = 5.1
check(
    "estimate respects $COXD_HOME/agy-prices.json override",
    abs(agyusage.estimate(unk) - 5.1) < 1e-9,
)

override_path.write_text("{not valid json", encoding="utf-8")
check(
    "malformed override file falls back to defaults",
    abs(agyusage.estimate(unk) - want_unk) < 1e-9,
)
override_path.write_text(
    json.dumps({"test-model-1": {"input": "bad", "output": 5}}),
    encoding="utf-8",
)
check(
    "invalid override entry falls back to defaults",
    abs(agyusage.estimate(unk) - want_unk) < 1e-9,
)
override_path.unlink(missing_ok=True)


# --- 2. Endpoint validation & status codes -----------------------------------
check("no agy_usage before any post", row("T1")["agy_usage"] is None)
r = post("T1")
check("valid post returns 200", r.status_code == 200 and r.json() == {"ok": True})
check(
    "stores one agy-usage event",
    len(usage_events("T1")) == 1 and usage_events("T1")[0]["data"] == OK,
)

u1 = row("T1")["agy_usage"]
check(
    "/api/tasks row carries agy_usage",
    u1 is not None
    and u1["tokens"] == 220_000
    and u1["duration_seconds"] == 360
    and u1["model"] == "gemini-4-hydrogen-high"
    and abs(u1["est_usd"] - 0.489) < 1e-9,
)

# Does not require waiting_agy (post arrives after the run)
check("accepted when agy task is not in waiting_agy", post("T2", unk).status_code == 200)
check("T2 stores agy-usage event", len(usage_events("T2")) == 1)

# 404 / 415 / 413 / 403
check("unknown task -> 404", post("nope").status_code == 404)
check("claude-lane task -> 404", post("C1").status_code == 404)
check(
    "wrong content type -> 415",
    c.post(
        "/api/task/T1/agy-usage",
        content="{}",
        headers={"content-type": "text/plain"},
    ).status_code == 415,
)
check(
    "oversized body -> 413",
    c.post(
        "/api/task/T1/agy-usage",
        content=json.dumps({**OK, "pad": "x" * 5000}),
        headers={"content-type": "application/json"},
    ).status_code == 413,
)
check(
    "foreign origin -> 403",
    post("T1", headers={"origin": "https://evil.example"}).status_code == 403,
)

# 400 validation errors
for name, bad in {
    "non-object list": [1, 2, 3],
    "non-object string": "hello",
    "missing input_tokens": {k: v for k, v in OK.items() if k != "input_tokens"},
    "missing model": {k: v for k, v in OK.items() if k != "model"},
    "negative tokens": {**OK, "input_tokens": -1},
    "negative duration": {**OK, "duration_seconds": -0.1},
    "huge tokens": {**OK, "output_tokens": 10**11 + 1},
    "huge duration": {**OK, "duration_seconds": 7 * 86400 + 1},
    "bool token": {**OK, "thinking_tokens": True},
    "bool duration": {**OK, "duration_seconds": False},
    "float token": {**OK, "cache_read_tokens": 1.5},
    "string token": {**OK, "input_tokens": "100"},
    "string duration": {**OK, "duration_seconds": "60"},
    "empty model": {**OK, "model": ""},
    "bad model chars": {**OK, "model": "bad model; rm"},
    "too-long model": {**OK, "model": "m" * 81},
    "non-string model": {**OK, "model": 42},
    "extra key": {**OK, "extra": 1},
}.items():
    check(f"rejects {name} with 400", post("T1", bad).status_code == 400)

nan_json = (
    '{"input_tokens":1,"output_tokens":1,"thinking_tokens":0,'
    '"cache_read_tokens":0,"duration_seconds":NaN,"model":"test-model-1"}'
)
inf_json = (
    '{"input_tokens":1,"output_tokens":1,"thinking_tokens":0,'
    '"cache_read_tokens":0,"duration_seconds":Infinity,"model":"test-model-1"}'
)
check(
    "rejects NaN with 400",
    c.post(
        "/api/task/T1/agy-usage",
        content=nan_json,
        headers={"content-type": "application/json"},
    ).status_code == 400,
)
check(
    "rejects Infinity with 400",
    c.post(
        "/api/task/T1/agy-usage",
        content=inf_json,
        headers={"content-type": "application/json"},
    ).status_code == 400,
)
check(
    "rejects invalid JSON with 400",
    c.post(
        "/api/task/T1/agy-usage",
        content="{nope",
        headers={"content-type": "application/json"},
    ).status_code == 400,
)
check("rejected posts did not add events", len(usage_events("T1")) == 1)


# --- 3. Card overwrite on fix round & month aggregation ----------------------
store.set_state("T1", "planning")
store.append_event("T1", "result", {"model": "claude-sonnet-5-5", "cost": 0.25})
store.add_cost("T1", 0.25)

before_spend = payload()["spend"]
before_model_spend = payload()["model_spend"]
before_claude_total = row("T1")["claude_total"]

round2 = {
    "input_tokens": 50_000,
    "output_tokens": 10_000,
    "thinking_tokens": 0,
    "cache_read_tokens": 0,
    "duration_seconds": 120,
    "model": "gemini-4-hydrogen-high",
}
# round2 est_usd = (50k * 1.50 + 10k * 7.50) / 1e6 = 0.075 + 0.075 = 0.15
check("second round post -> 200", post("T1", round2).status_code == 200)
u_latest = row("T1")["agy_usage"]
check(
    "latest event overwrites card agy_usage",
    u_latest is not None
    and u_latest["tokens"] == 60_000
    and u_latest["duration_seconds"] == 120
    and abs(u_latest["est_usd"] - 0.15) < 1e-9,
)

# Insert a pre-month event and archive T2 to verify month filtering + archived inclusion
tz = payload()["tz"]
m_start = stepspend.month_start(time.time(), tz)
with store._conn() as conn:
    conn.execute(
        "INSERT INTO events(task_id,ts,kind,data) VALUES(?,?,?,?)",
        ("T1", m_start - 3600, "agy-usage", json.dumps(OK)),
    )
store.set_state("T2", "landed")
store.archive_task("T2")

p_now = payload()
ag_sp = p_now["agy_spend"]
# Current-month: T1 round1 (220k, $0.489), T1 round2 (60k, $0.15), T2 archived (1.8M, want_unk)
want_tokens = 220_000 + 60_000 + 1_800_000
want_usd = 0.489 + 0.15 + want_unk
check(
    "month agy_spend sums current-month runs (including archived) and ignores pre-month events",
    ag_sp["runs"] == 3
    and ag_sp["tokens"] == want_tokens
    and abs(ag_sp["est_usd"] - want_usd) < 1e-9,
)
check(
    "spend, model_spend and claude_total unchanged by agy-usage",
    p_now["spend"] == before_spend
    and p_now["model_spend"] == before_model_spend
    and row("T1")["claude_total"] == before_claude_total,
)

# UI JS check
html = c.get("/").text
check(
    "card and spend JS contain subscription label and title",
    "(subscription)" in html
    and 'title="≈ API price (paid by subscription)"' in html
    and "agy (subscription):" in html
    and "renderSpend(s.model_spend,s.phase_bars,s.cost_trend,s.agy_spend)" in html,
)

print("FAILED" if fails else "all passed")
sys.exit(1 if fails else 0)

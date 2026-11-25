"""Tests for agy queue 4/8: board Next-Up strip, agy_queue payload, and queue-aware card cmd.

    cd coxd && python test_board_agy_nextup.py
(invented data only; no network or agy calls.)
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-board-nextup-")
os.environ.pop("COXD_OWNER_USER", None)
os.environ.pop("COXD_PROXY_SECRET", None)

import agy_handoff  # noqa: E402
import board  # noqa: E402
import ci_triage  # noqa: E402
import store  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)
    assert cond, name


@contextmanager
def _patch(obj, attr: str, value):
    old = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, old)


def _clear_tasks() -> None:
    with store._conn() as c:
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM tasks")
    board._AGY_QUEUE_CACHE.clear()
    agy_handoff._PR_FILES_CACHE.clear()
    agy_handoff._ISSUE_LABEL_CACHE.clear()


EXISTING_ROW_KEYS = {
    "id", "repo", "state", "reason", "cost", "pr_url", "stage", "lane", "stages",
    "plan_md", "plan_cost", "claude_split", "claude_total", "agy_tokens",
    "agy_pickup", "waiting_since", "active", "needs_you", "issue_url",
    "archivable", "archived", "resumable", "agy_cmd", "agy_round", "agy_status",
    "choice", "quota_wait_at", "approve_auto", "deployable", "last", "timing",
    "models", "resume_at", "budget", "over_budget",
}


def test_empty_queue_and_claude_lane_unchanged() -> None:
    _clear_tasks()
    p0 = board._tasks_payload()
    check("empty store -> empty agy_queue", p0["agy_queue"] == {"ready": [], "waiting": []})

    # Only a coxd-ready Claude task
    board._AGY_QUEUE_CACHE.clear()
    store.create_task(
        "claude-1",
        "widgets",
        "Implement widget\n\nGitHub issue: https://github.com/acme/widgets/issues/50",
        "/tmp/wt-c1",
        repo_path="/tmp/widgets",
        lane=None,
    )
    store.set_state("claude-1", "working")
    p1 = board._tasks_payload()
    check("only Claude tasks -> empty agy_queue", p1["agy_queue"] == {"ready": [], "waiting": []})
    crow = next(r for r in p1["tasks"] if r["id"] == "claude-1")
    check("Claude task agy_cmd is None", crow["agy_cmd"] is None and crow.get("agy_wait") is None)
    check(
        "Claude task preserves all existing keys",
        EXISTING_ROW_KEYS.issubset(set(crow.keys()))
        and set(crow.keys()) - EXISTING_ROW_KEYS <= {"agy_wait", "ci_parity"},
    )
    check(
        "Claude task existing field values unchanged",
        crow["lane"] == "claude"
        and crow["stages"] is None
        and crow["plan_md"] is None
        and crow["plan_cost"] is None
        and crow["claude_split"] is None
        and crow["claude_total"] is None
        and crow["agy_tokens"] is None
        and crow["agy_pickup"] is None
        and crow["waiting_since"] is None
        and crow["agy_round"] is None
        and crow["agy_status"] is None
        and crow["choice"] is None
        and crow["quota_wait_at"] is None,
    )

    # queue_inputs exception -> empty queue, payload still builds
    board._AGY_QUEUE_CACHE.clear()

    def boom():
        raise RuntimeError("gh failed")

    with _patch(agy_handoff, "queue_inputs", boom):
        p_err = board._tasks_payload()
    check(
        "queue_inputs exception yields empty queue and payload builds",
        p_err["agy_queue"] == {"ready": [], "waiting": []}
        and any(r["id"] == "claude-1" for r in p_err["tasks"]),
    )


def test_payload_ready_waiting_and_cards() -> None:
    _clear_tasks()

    b_ready = "Add strip helper\n\nGitHub issue: https://github.com/acme/widgets/issues/101"
    b_blocked = "Update shared board\n\nGitHub issue: https://github.com/acme/widgets/issues/102"
    b_active = "In-flight change\n\nGitHub issue: https://github.com/acme/widgets/issues/103"

    store.create_task(
        "t-ready", "widgets", b_ready, "/tmp/wt1", repo_path="/tmp/widgets", lane="agy-handoff"
    )
    store.set_state("t-ready", "waiting_agy")
    store.append_event("t-ready", "plan", {"round": 1, "files": ["coxd/free.py", "coxd/extra.py"]})

    store.create_task(
        "t-blocked", "widgets", b_blocked, "/tmp/wt2", repo_path="/tmp/widgets", lane="agy-handoff"
    )
    store.set_state("t-blocked", "waiting_agy")
    store.append_event("t-blocked", "plan", {"round": 1, "files": ["coxd/shared.py"]})

    store.create_task(
        "t-active", "widgets", b_active, "/tmp/wt3", repo_path="/tmp/widgets", lane="agy-handoff"
    )
    store.set_state("t-active", "gating")
    store.append_event("t-active", "plan", {"round": 1, "files": ["coxd/shared.py"]})

    def fake_gh(args: list[str], timeout: float = 10.0) -> tuple[int, str]:
        if args[:2] == ["pr", "list"]:
            return 0, json.dumps([])
        if args[:2] == ["issue", "view"]:
            num = int(args[2])
            lbs = [{"name": "size:S"}] if num == 101 else [{"name": "size:M"}]
            return 0, json.dumps({"labels": lbs})
        raise AssertionError(f"unexpected gh call: {args}")

    with (
        _patch(ci_triage, "repo_slug", lambda _p: "acme/widgets"),
        _patch(agy_handoff, "_gh", fake_gh),
    ):
        payload = board._tasks_payload()

    q = payload["agy_queue"]
    check("agy_queue has 1 ready and 1 waiting", len(q["ready"]) == 1 and len(q["waiting"]) == 1)

    r0 = q["ready"][0]
    check(
        "ready row keys and values",
        set(r0.keys()) == {"id", "repo", "issue", "title", "cmd", "model", "files_count"}
        and r0["id"] == "t-ready"
        and r0["repo"] == "widgets"
        and r0["issue"] == 101
        and r0["title"] == "Add strip helper"
        and r0["model"] == "gemini-4-hydrogen-high"
        and r0["cmd"] == "agy-next t-ready --model gemini-4-hydrogen-high"
        and r0["files_count"] == 2,
    )

    w0 = q["waiting"][0]
    check(
        "waiting row keys and blocked_by",
        set(w0.keys()) == {"id", "repo", "issue", "title", "blocked_by"}
        and w0["id"] == "t-blocked"
        and w0["repo"] == "widgets"
        and w0["issue"] == 102
        and w0["title"] == "Update shared board"
        and w0["blocked_by"] == ["#103"],
    )

    cards = {r["id"]: r for r in payload["tasks"]}
    check(
        "ready card agy_cmd has --model and agy_wait is None",
        cards["t-ready"]["agy_cmd"] == "agy-next t-ready --model gemini-4-hydrogen-high"
        and cards["t-ready"]["agy_wait"] is None,
    )
    check(
        "blocked card agy_cmd is None and agy_wait has waits for #103 to merge",
        cards["t-blocked"]["agy_cmd"] is None
        and cards["t-blocked"]["agy_wait"] == "waits for #103 to merge",
    )

    # Fallback when a waiting_agy task has no row in the queue
    store.create_task(
        "t-norow",
        "widgets",
        "Fallback task",
        "/tmp/wt4",
        repo_path="/tmp/widgets",
        lane="agy-handoff",
    )
    store.set_state("t-norow", "waiting_agy")
    board._AGY_QUEUE_CACHE.clear()
    with _patch(agy_handoff, "queue_inputs", lambda: ([], {}, {}, {})):
        p_fb = board._tasks_payload()
    fb_card = next(r for r in p_fb["tasks"] if r["id"] == "t-norow")
    check(
        "waiting_agy task without queue row falls back to agy-next <id>",
        fb_card["agy_cmd"] == "agy-next t-norow" and fb_card["agy_wait"] is None,
    )

    # _agy_queue_view does not mutate input rows
    raw_rows = [
        {
            "id": "a", "repo": "widgets", "issue": 1, "title": "One",
            "files": ["x.py"], "status": "ready", "blocked_by": [],
            "model": "gemini-4-argon-high", "cmd": "agy-next a --model gemini-4-argon-high",
        },
        {
            "id": "b", "repo": "widgets", "issue": 2, "title": "Two",
            "files": ["y.py"], "status": "blocked", "blocked_by": ["#1"],
            "model": "gemini-4-argon-high", "cmd": "agy-next b --model gemini-4-argon-high",
        },
    ]
    snap = copy.deepcopy(raw_rows)
    view = board._agy_queue_view(raw_rows)
    view["waiting"][0]["blocked_by"].append("#999")
    check("_agy_queue_view does not mutate input rows", raw_rows == snap)


def test_cache_ttl() -> None:
    _clear_tasks()
    store.create_task(
        "t-cache",
        "widgets",
        "Cache test\n\nGitHub issue: https://github.com/acme/widgets/issues/77",
        "/tmp/wt-cache",
        repo_path="/tmp/widgets",
        lane="agy-handoff",
    )
    store.set_state("t-cache", "waiting_agy")
    store.append_event("t-cache", "plan", {"round": 1, "files": ["coxd/board.py"]})

    calls = 0

    def fake_inputs():
        nonlocal calls
        calls += 1
        tasks = store.list_tasks()
        evs = {t["id"]: store.events(t["id"]) for t in tasks}
        return tasks, evs, {}, {"t-cache": {"size:S"}}

    board._AGY_QUEUE_CACHE.clear()
    with _patch(agy_handoff, "queue_inputs", fake_inputs):
        p1 = board._tasks_payload()
        p2 = board._tasks_payload()
        check("two _tasks_payload() calls within TTL invoke queue_inputs once", calls == 1)
        check(
            "cached result preserved across second call",
            p1["agy_queue"] == p2["agy_queue"] and len(p1["agy_queue"]["ready"]) == 1,
        )
        board._AGY_QUEUE_CACHE.clear()
        board._tasks_payload()
        check("cache clear hook triggers fresh queue_inputs call", calls == 2)


def test_failure_cached_and_cache_only_read() -> None:
    _clear_tasks()
    calls = 0

    def boom():
        nonlocal calls
        calls += 1
        raise RuntimeError("gh failed")

    with _patch(agy_handoff, "queue_inputs", boom):
        check("failed lookup yields []", board._agy_queue_rows() == [])
        board._agy_queue_rows()
        board._tasks_payload()
        check("failure cached for the TTL (one queue_inputs call)", calls == 1)
    _clear_tasks()
    with _patch(agy_handoff, "queue_inputs", boom):
        board._tasks_payload(fetch_queue=False)
    check("fetch_queue=False never calls queue_inputs", calls == 1)


def test_escaping_and_readonly() -> None:
    _clear_tasks()
    xss_title = "<script>alert(1)</script>"
    xss_repo = "widgets<script>"
    store.create_task(
        "t-xss",
        xss_repo,
        f"{xss_title}\n\nGitHub issue: https://github.com/acme/widgets/issues/88",
        "/tmp/wt-xss",
        repo_path="/tmp/widgets",
        lane="agy-handoff",
    )
    store.set_state("t-xss", "waiting_agy")
    store.append_event("t-xss", "plan", {"round": 1, "files": ["coxd/board.py"]})

    board._AGY_QUEUE_CACHE.clear()
    with (
        _patch(ci_triage, "repo_slug", lambda _p: None),
        _patch(agy_handoff, "_gh", lambda *_a, **_k: (1, "")),
    ):
        client = TestClient(board.app, base_url="http://localhost:8791")
        api_resp = client.get("/api/tasks")
        html_resp = client.get("/")
        post_queue = client.post("/api/agy/queue")

    check("GET /api/tasks -> 200", api_resp.status_code == 200)
    data = api_resp.json()
    r0 = data["agy_queue"]["ready"][0]
    check(
        "<script> payload preserved in JSON data only",
        r0["title"] == xss_title and r0["repo"] == xss_repo,
    )
    html = html_resp.text
    check(
        "index HTML does not contain raw task <script>alert(1)",
        "<script>alert(1)</script>" not in html,
    )
    check(
        "HTML uses esc() in renderNextUp and agyCardHtml",
        "id=nextup hidden" in html
        and "function renderNextUp(q)" in html
        and "renderNextUp(s.agy_queue)" in html
        and "esc(r.repo)" in html
        and "esc(r.title)" in html
        and "esc(r.cmd)" in html
        and "esc(r.model)" in html
        and "esc(w.repo)" in html
        and "esc(w.title)" in html
        and "esc(t.agy_wait)" in html
        and "function copyCmd(btn)" in html,
    )
    check("POST /api/agy/queue is still 405", post_queue.status_code == 405)

    post_routes = [
        r.path for r in board.app.routes
        if hasattr(r, "methods") and r.methods and "POST" in r.methods
    ]
    expected_post_routes = [
        "/api/dispatch",
        "/api/task/{tid}/retry",
        "/api/task/{tid}/agy-choice",
        "/api/task/{tid}/agy-status",
        "/api/task/{tid}/stop",
        "/api/task/{tid}/resume",
        "/api/task/{tid}/deploy",
        "/api/repo/{repo}/approve-auto-merge",
        "/api/task/{tid}/archive",
        "/api/task/{tid}/unarchive",
        "/api/restart",
        "/api/factory",
        "/api/suggestion/{sid}/approve",
        "/api/suggestion/{sid}/reject",
    ]
    check(
        "no new POST routes added",
        set(expected_post_routes) <= set(post_routes),
    )


if __name__ == "__main__":
    for fn in (
        test_empty_queue_and_claude_lane_unchanged,
        test_payload_ready_waiting_and_cards,
        test_cache_ttl,
        test_failure_cached_and_cache_only_read,
        test_escaping_and_readonly,
    ):
        fn()
    print("FAILED" if fails else "all passed")
    sys.exit(1 if fails else 0)

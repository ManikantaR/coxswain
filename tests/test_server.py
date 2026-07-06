"""Dashboard server: pure payload functions + a live HTTP smoke test (stdlib)."""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from cox import server, store
from cox.model import DispatchPath, NeedsHumanReason, TaskMeta, TaskState


def _mk(tid: str, state: TaskState, reason: NeedsHumanReason | None = None) -> None:
    store.save_meta(
        TaskMeta(
            id=tid, repo="repo", worktree="/tmp/wt", branch=f"cox/{tid}",
            lane="claude", model="sonnet:medium", path=DispatchPath.FULL,
            state=state, reason=reason,
        )
    )


def test_tasks_payload_orders_needs_you_first_and_totals_cost():
    _mk("t-landed", TaskState.LANDED)
    _mk("t-working", TaskState.WORKING)
    _mk("t-needs", TaskState.NEEDS_HUMAN, NeedsHumanReason.GATE_RED)
    from cox.model import CostEntry

    def cost(v: float) -> CostEntry:
        return CostEntry(phase="implement", tokens_in=1, tokens_out=1, cost_usd=v)

    store.append_cost("t-landed", cost(0.5))
    store.append_cost("t-working", cost(0.25))

    p = server.tasks_payload()
    assert p["needs_you"] == 1
    assert p["active"] == 1
    assert p["total_cost_usd"] == pytest.approx(0.75)
    # needs-you sorts before active before the rest
    assert p["tasks"][0]["id"] == "t-needs"
    assert p["tasks"][1]["id"] == "t-working"
    assert p["tasks"][0]["needs_you"] is True


def test_feed_payload_uses_renderer():
    _mk("t-feed", TaskState.WORKING)
    log = store.task_data_dir("t-feed") / "worker.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}
        ),
        encoding="utf-8",
    )
    assert server.feed_payload("t-feed")["feed"] == ["· hello"]


def test_stop_task_marks_failed_when_no_live_pid():
    _mk("t-stop", TaskState.WORKING)
    out = server.stop_task("t-stop")
    assert out["state"] == "failed"
    assert store.load_meta("t-stop").state is TaskState.FAILED


def test_http_requires_token_and_serves_tasks():
    _mk("t-http", TaskState.WORKING)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._make_handler("secret"))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # no token -> 401
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tasks", timeout=5)
        assert ei.value.code == 401
        # with token -> the task shows up
        body = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/tasks?t=secret", timeout=5
        ).read()
        data = json.loads(body)
        assert any(t["id"] == "t-http" for t in data["tasks"])
    finally:
        httpd.shutdown()

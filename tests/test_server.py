"""Dashboard server: pure payload functions + a live HTTP smoke test (stdlib)."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

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


def test_dispatch_task_validates_and_surfaces_errors():
    # missing fields -> error, no spawn
    assert "error" in server.dispatch_task({"repo": "", "title": ""})
    # a real dispatch is monkeypatched at the dispatch module boundary
    assert server.dispatch_task({"title": "x"})["error"]  # no repo


def test_dispatch_task_calls_dispatch(monkeypatch):
    from cox import dispatch as disp
    from cox.model import DispatchPath, TaskMeta

    captured = {}

    def fake_dispatch(**kw):
        captured.update(kw)
        return TaskMeta(
            id="repo-x-2601010000", repo="repo", worktree="/w", branch="cox/x",
            lane=kw["lane"], model="sonnet:medium", path=kw["path"], state=TaskState.WORKING,
        )

    monkeypatch.setattr(disp, "dispatch", fake_dispatch)
    out = server.dispatch_task(
        {"repo": "~/repo/coxswain", "title": "do it", "lane": "codex", "model": "opus:high"}
    )
    assert out["id"] == "repo-x-2601010000"
    assert captured["lane"] == "codex"
    assert captured["model_override"] == "opus:high"
    assert captured["path"] is DispatchPath.FULL


class _FakeSocket:
    def __init__(self, request: bytes) -> None:
        self._rfile = io.BytesIO(request)
        self._wfile = io.BytesIO()

    def makefile(self, mode: str, *args: object, **kwargs: object) -> io.BytesIO:
        del args, kwargs
        if "r" in mode:
            return self._rfile
        if "w" in mode:
            return self._wfile
        raise ValueError(mode)

    def sendall(self, data: bytes) -> None:
        self._wfile.write(data)

    def close(self) -> None:
        pass

    def value(self) -> bytes:
        return self._wfile.getvalue()


def _http_get(path: str) -> tuple[int, dict[str, object]]:
    handler_cls = server._make_handler("secret")
    sock = _FakeSocket(
        f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
    )
    handler_cls(sock, ("127.0.0.1", 0), SimpleNamespace(server_name="localhost", server_port=80))
    head, body = sock.value().split(b"\r\n\r\n", 1)
    code = int(head.splitlines()[0].split()[1])
    return code, json.loads(body)


def test_http_requires_token_and_serves_tasks():
    _mk("t-http", TaskState.WORKING)
    code, body = _http_get("/api/tasks")
    assert code == 401
    assert body == {"error": "unauthorized"}

    code, body = _http_get("/api/tasks?t=secret")
    assert code == 200
    tasks = body["tasks"]
    assert isinstance(tasks, list)
    assert any(task["id"] == "t-http" for task in tasks)

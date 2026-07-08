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


def test_tasks_payload_reports_pipeline_stage():
    from cox.model import CostEntry

    _mk("t-work2", TaskState.GATING)
    _mk("t-land2", TaskState.LANDED)
    _mk("t-review", TaskState.NEEDS_HUMAN, NeedsHumanReason.REVIEW_FINDINGS)
    # a resumed fix round should be counted onto the review stage
    store.append_cost(
        "t-review", CostEntry(phase="fix", tokens_in=1, tokens_out=1, cost_usd=0.1)
    )

    by_id = {t["id"]: t for t in server.tasks_payload()["tasks"]}
    assert server.tasks_payload()["stages"] == ["Code", "Gate", "Review", "PR", "Merged"]
    assert by_id["t-work2"]["stage"] == {"i": 1, "status": "active", "fix_rounds": 0}
    assert by_id["t-land2"]["stage"] == {"i": 4, "status": "done", "fix_rounds": 0}
    assert by_id["t-review"]["stage"] == {"i": 2, "status": "error", "fix_rounds": 1}


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


def test_dispatch_task_passes_review_slot(monkeypatch):
    from cox import dispatch as disp
    from cox.model import TaskMeta

    captured = {}

    def fake_dispatch(**kw):
        captured.update(kw)
        return TaskMeta(
            id="r-x-1", repo="repo", worktree="/w", branch="cox/x", lane=kw["lane"],
            model="sonnet:medium", path=kw["path"], state=TaskState.WORKING,
            review_lane=kw["review_lane"], review_model=kw["review_model"],
        )

    monkeypatch.setattr(disp, "dispatch", fake_dispatch)
    out = server.dispatch_task(
        {"repo": "~/r", "title": "t", "lane": "claude",
         "review_lane": "codex", "review_model": "gpt-5.4:high"}
    )
    assert captured["review_lane"] == "codex"
    assert captured["review_model"] == "gpt-5.4:high"
    assert out["review"] == "codex/gpt-5.4:high"
    # blank review slot -> None (falls back to the reviewer default) and "default" label
    server.dispatch_task({"repo": "~/r", "title": "t", "lane": "claude"})
    assert captured["review_lane"] is None and captured["review_model"] is None


def _fake_dispatch(captured):
    from cox.model import DispatchPath, TaskMeta

    def fake(**kw):
        captured.update(kw)
        return TaskMeta(
            id="repo-x-2601010000", repo="repo", worktree="/w", branch="cox/x",
            lane=kw["lane"], model="m:e", path=DispatchPath.FULL, state=TaskState.WORKING,
        )

    return fake


def test_dispatch_effort_tunes_lane_default_model(monkeypatch):
    from cox import dispatch as disp

    captured = {}
    monkeypatch.setattr(disp, "dispatch", _fake_dispatch(captured))
    # effort with no explicit model -> lane default model + chosen effort
    server.dispatch_task({"repo": "~/r", "title": "t", "lane": "claude", "effort": "high"})
    assert captured["model_override"] == "claude-sonnet-4-6:high"


def test_dispatch_from_issue_fills_title_and_body(monkeypatch):
    from cox import dispatch as disp
    from cox import proc

    def fake_run(cmd, **kw):
        assert cmd[:3] == ["gh", "issue", "view"]
        payload = json.dumps(
            {"number": 7, "title": "Fix the parser", "body": "It crashes on empty input.",
             "url": "https://github.com/o/r/issues/7"}
        )
        return proc.ProcResult(0, payload, "")

    monkeypatch.setattr(proc, "run", fake_run)
    captured = {}
    monkeypatch.setattr(disp, "dispatch", _fake_dispatch(captured))
    out = server.dispatch_task({"repo": "~/r", "issue": "https://github.com/o/r/issues/7"})
    assert "id" in out
    assert captured["title"] == "Fix the parser"
    assert "It crashes on empty input." in captured["body"]
    assert "issues/7" in captured["body"]  # issue link appended


def test_list_issues_surfaces_gh_error(monkeypatch):
    from cox import proc

    def boom(cmd, **kw):
        raise proc.BosunProcError(cmd, 1, "", "not a gh repo")

    monkeypatch.setattr(proc, "run", boom)
    assert "error" in server.list_issues("~/r")


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

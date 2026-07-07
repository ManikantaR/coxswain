"""`cox serve` — a glance-and-alert dashboard over the task state (stdlib only).

Not a live movie: a calm board you check occasionally and that surfaces what
needs you (needs-human tasks first), the live cost, and each task's narrated
activity feed (reusing cox.render). Serves one responsive page for desktop AND
phone on the home LAN, plus STOP / pause controls. SSE pushes updates.

Design (see coxswain-observability-direction memory):
- stdlib http.server only — no deps, so it ports to Windows/work.
- Shared-token auth (`?t=<token>` on every route) so it isn't wide open on the
  LAN. The token is minted at startup and printed in the URL to bookmark.
- Logic lives in pure payload functions (unit-tested); the handler is a thin shell.
"""

from __future__ import annotations

import json
import os
import signal
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import home, render, store
from .model import NeedsHumanReason, TaskState

_ACTIVE = {TaskState.WORKING, TaskState.GATING, TaskState.FIXING}
_NEEDS_YOU = {TaskState.NEEDS_HUMAN, TaskState.PR_OPEN}
_TEMPLATE = Path(__file__).parent / "templates" / "dashboard.html"


def _last_status(task_id: str) -> str:
    p = store.task_data_dir(task_id) / "status.log"
    if not p.exists():
        return ""
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def tasks_payload() -> dict:
    """Everything the board renders: per-task summary + fleet totals."""
    rows = []
    total_cost = 0.0
    have_cost = False
    for tid in store.list_task_ids():
        m = store.load_meta(tid)
        _, _, cost = store.cost_total(tid)
        if cost is not None:
            total_cost += cost
            have_cost = True
        rows.append(
            {
                "id": m.id,
                "repo": m.repo,
                "state": m.state.value,
                "reason": m.reason.value if m.reason else None,
                "lane": m.lane,
                "model": m.model,
                "cost_usd": cost,
                "pr_url": m.pr_url,
                "last_status": _last_status(tid),
                "active": m.state in _ACTIVE,
                "needs_you": m.state in _NEEDS_YOU,
            }
        )
    # needs-you first, then active, then the rest — the "alert" ordering
    rows.sort(key=lambda r: (not r["needs_you"], not r["active"], r["id"]), reverse=False)
    return {
        "paused": home.is_paused(),
        "total_cost_usd": total_cost if have_cost else None,
        "needs_you": sum(1 for r in rows if r["needs_you"]),
        "active": sum(1 for r in rows if r["active"]),
        "tasks": rows,
    }


def feed_payload(task_id: str, n: int = 20) -> dict:
    log = store.task_data_dir(task_id) / "worker.log"
    return {"id": task_id, "feed": render.summarize_stream(log, n=n)}


def stop_task(task_id: str) -> dict:
    """Captain's STOP: kill the worker process and mark the task failed."""
    meta = store.load_meta(task_id)
    pid_path = store.task_state_dir(task_id) / "pid"
    killed = False
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
            killed = True
        except (ValueError, ProcessLookupError, OSError):
            pass
    meta = replace(meta, state=TaskState.FAILED, reason=NeedsHumanReason.WORKER_ERROR)
    store.save_meta(meta)
    store.append_status(task_id, "failed: stopped by captain")
    return {"stopped": killed, "state": meta.state.value}


def set_paused(paused: bool) -> dict:
    home.set_paused(paused)
    return {"paused": paused}


def dispatch_task(payload: dict) -> dict:
    """Captain dispatch from the UI. Manual only (never auto-dispatch, DESIGN).

    Mirrors `cox dispatch`; any dispatch failure (paused, worker cap, unpinned
    model, bad repo) is surfaced to the UI rather than raised."""
    from . import dispatch as disp
    from .model import DispatchPath

    repo = str(payload.get("repo") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not repo or not title:
        return {"error": "repo and title are required"}
    lane = str(payload.get("lane") or "claude")
    path_val = str(payload.get("path") or "full")
    model = str(payload.get("model") or "").strip() or None
    try:
        meta = disp.dispatch(
            repo_path=Path(repo).expanduser(),
            title=title,
            body=str(payload.get("body") or title),
            path=DispatchPath(path_val),
            lane=lane,
            model_override=model,
        )
    except Exception as e:  # noqa: BLE001 - surface every dispatch failure to the UI
        return {"error": f"{type(e).__name__}: {e}"}
    return {"id": meta.id, "lane": meta.lane, "model": meta.model, "state": meta.state.value}


# --- HTTP shell -------------------------------------------------------------


def _make_handler(token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:  # quiet; no per-request stderr spam
            pass

        def _authed(self, q: dict[str, list[str]]) -> bool:
            return q.get("t", [""])[0] == token

        def _json(self, obj: object, code: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/" and self._authed(q):
                html = _TEMPLATE.read_text(encoding="utf-8").replace("__TOKEN__", token)
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not self._authed(q):
                self._json({"error": "unauthorized"}, 401)
                return
            if u.path == "/api/tasks":
                self._json(tasks_payload())
            elif u.path.startswith("/api/task/") and u.path.endswith("/feed"):
                tid = u.path[len("/api/task/") : -len("/feed")]
                self._json(feed_payload(tid))
            elif u.path == "/events":
                self._sse()
            else:
                self._json({"error": "not found"}, 404)

        def _sse(self) -> None:
            import time

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    payload = json.dumps(tasks_payload())
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(2)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return  # client closed — normal

        def do_POST(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._authed(q):
                self._json({"error": "unauthorized"}, 401)
                return
            if u.path == "/api/pause":
                self._json(set_paused(True))
            elif u.path == "/api/resume":
                self._json(set_paused(False))
            elif u.path.startswith("/api/task/") and u.path.endswith("/stop"):
                tid = u.path[len("/api/task/") : -len("/stop")]
                self._json(stop_task(tid))
            elif u.path == "/api/dispatch":
                out = dispatch_task(self._read_json())
                self._json(out, 400 if out.get("error") else 200)
            else:
                self._json({"error": "not found"}, 404)

        def _read_json(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n) if n > 0 else b""
                obj = json.loads(raw or b"{}")
                return obj if isinstance(obj, dict) else {}
            except (ValueError, json.JSONDecodeError):
                return {}

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8787, token: str | None = None) -> None:
    """Start the dashboard server (blocking)."""
    import secrets

    home.ensure_home()
    token = token or secrets.token_urlsafe(12)
    httpd = ThreadingHTTPServer((host, port), _make_handler(token))
    shown = host if host != "0.0.0.0" else _lan_ip()  # noqa: S104 (intentional LAN bind)
    print(f"cox dashboard → http://{shown}:{port}/?t={token}")
    print("  (bookmark that URL on your phone; Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


def _lan_ip() -> str:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()

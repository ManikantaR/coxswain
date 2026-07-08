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

# The pipeline the human watches loop: Code → Gate → Review → PR → Merged.
# The stepper on each card lights the stage a task is in; a review-findings loop
# (FIXING) sits back at Review with a fix-round counter — that's the "looping".
STAGES = ["Code", "Gate", "Review", "PR", "Merged"]

# Which stage a live state occupies (index into STAGES).
_STATE_STAGE = {
    TaskState.QUEUED: 0,
    TaskState.WORKING: 0,
    TaskState.GATING: 1,
    TaskState.FIXING: 2,
    TaskState.PR_OPEN: 3,
    TaskState.LANDED: 4,
}
# When a task stalls needing a human, the reason tells us which stage went red.
_REASON_STAGE = {
    NeedsHumanReason.WORKER_ERROR: 0,
    NeedsHumanReason.WORKER_STALE: 0,
    NeedsHumanReason.RATE_LIMITED: 0,
    NeedsHumanReason.GATE_RED: 1,
    NeedsHumanReason.EVIDENCE_MISSING: 1,
    NeedsHumanReason.REVIEW_FINDINGS: 2,
    NeedsHumanReason.PUSH_REJECTED: 3,
    NeedsHumanReason.PR_ERROR: 3,
    NeedsHumanReason.CI_RED: 3,
}


def _stage(meta, fix_rounds: int) -> dict:
    """Where this task sits on the Code→Merged pipeline, for the card stepper.

    Returns {i, status, fix_rounds}: stages before `i` render done, stage `i`
    renders `status` (active | error | done), stages after render pending.
    """
    state = meta.state
    if state is TaskState.LANDED:
        return {"i": 4, "status": "done", "fix_rounds": fix_rounds}
    if state is TaskState.NEEDS_HUMAN:
        i = _REASON_STAGE.get(meta.reason, 0)
        return {"i": i, "status": "error", "fix_rounds": fix_rounds}
    if state is TaskState.FAILED:
        return {"i": 0, "status": "error", "fix_rounds": fix_rounds}
    i = _STATE_STAGE.get(state, 0)
    return {"i": i, "status": "active", "fix_rounds": fix_rounds}


def _fix_rounds(task_id: str) -> int:
    """How many resumed fix rounds this task has run (cost.jsonl phases)."""
    return sum(1 for e in store.read_cost(task_id) if "fix" in e.phase)


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
                "stage": _stage(m, _fix_rounds(tid)),
                "review": _review_label(m),
            }
        )
    # needs-you first, then active, then the rest — the "alert" ordering
    rows.sort(key=lambda r: (not r["needs_you"], not r["active"], r["id"]), reverse=False)
    return {
        "paused": home.is_paused(),
        "total_cost_usd": total_cost if have_cost else None,
        "needs_you": sum(1 for r in rows if r["needs_you"]),
        "active": sum(1 for r in rows if r["active"]),
        "stages": STAGES,
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


def list_repos() -> dict:
    """Git repos in the clone-root, for the dispatch picker (DESIGN-VNEXT D17)."""
    from . import repos

    return {
        "root": str(repos.repo_root()),
        "repos": [
            {"name": r.name, "path": str(r.path), "trusted": r.trusted}
            for r in repos.list_local_repos()
        ],
    }


def add_repo(ref: str) -> dict:
    """Resolve a picker entry — clone a git URL (deduped, defanged) or accept a
    local path. A freshly cloned repo comes back needs_trust=True."""
    from . import repos

    ref = (ref or "").strip()
    if not ref:
        return {"error": "paste a git URL or a local repo path/name"}
    try:
        res = repos.resolve(ref)
    except Exception as e:  # noqa: BLE001 - surface any clone/resolve failure to the UI
        return {"error": f"{type(e).__name__}: {e}"}
    return {
        "path": str(res.path),
        "name": res.path.name,
        "cloned": res.cloned,
        "trusted": res.trusted,
        "needs_trust": not res.trusted,
    }


def trust_repo(path: str) -> dict:
    """Captain confirms a freshly cloned repo so dispatch will accept it."""
    from . import repos

    if not (path or "").strip():
        return {"error": "repo path required"}
    repos.mark_trusted(Path(path))
    return {"path": path, "trusted": True}


def list_issues(repo_path: str) -> dict:
    """Open GitHub issues for the repo, for the dispatch picker (via `gh`)."""
    from . import proc

    if not repo_path.strip():
        return {"error": "repo path required"}
    try:
        r = proc.run(
            ["gh", "issue", "list", "--json", "number,title,url", "--limit", "30"],
            cwd=Path(repo_path).expanduser(),
        )
    except proc.BosunProcError as e:
        return {"error": f"gh issue list failed: {(e.err or e.out).strip()[:200]}"}
    try:
        return {"issues": json.loads(r.out or "[]")}
    except json.JSONDecodeError:
        return {"error": "could not parse gh output"}


def resolve_issue(ref: str, repo_path: str | None) -> dict:
    """Fetch an issue's title + body by URL or number (via `gh issue view`)."""
    from . import proc

    ref = ref.strip()
    # a bare number needs repo context; a URL is self-contained
    cwd = Path(repo_path).expanduser() if (repo_path and "://" not in ref) else None
    try:
        r = proc.run(
            ["gh", "issue", "view", ref, "--json", "number,title,body,url"], cwd=cwd
        )
    except proc.BosunProcError as e:
        return {"error": f"gh issue view failed: {(e.err or e.out).strip()[:200]}"}
    try:
        d = json.loads(r.out)
    except json.JSONDecodeError:
        return {"error": "could not parse gh output"}
    return {"title": d.get("title", ""), "body": d.get("body") or "", "url": d.get("url", "")}


def dispatch_task(payload: dict) -> dict:
    """Captain dispatch from the UI. Manual only (never auto-dispatch, DESIGN).

    Accepts either typed title/body OR a GitHub `issue` (URL/number) whose title
    and body populate the task. `effort` (low|medium|high) tunes the lane's default
    model; an explicit `model` override still wins. Any dispatch failure (paused,
    worker cap, unpinned model, bad repo, gh error) is surfaced to the UI."""
    from . import dispatch as disp
    from . import models
    from .model import DispatchPath

    repo = str(payload.get("repo") or "").strip()
    if not repo:
        return {"error": "repo path is required"}
    lane = str(payload.get("lane") or "claude")
    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or "").strip()

    issue_ref = str(payload.get("issue") or "").strip()
    if issue_ref:
        info = resolve_issue(issue_ref, repo)
        if info.get("error"):
            return info
        title = title or info["title"]
        issue_block = f"{info['body']}\n\nGitHub issue: {info['url']}".strip()
        body = f"{body}\n\n{issue_block}".strip() if body else issue_block
    if not title:
        return {"error": "provide a title or a GitHub issue"}

    # model precedence: explicit override > lane default with chosen effort
    model = str(payload.get("model") or "").strip() or None
    effort = str(payload.get("effort") or "").strip() or None
    if not model and effort:
        default = models.resolve("implementer", lane=lane)
        model = f"{default.model}:{effort}"

    # review slot (DESIGN-VNEXT D14): pinned independently; blank = reviewer default
    review_lane = str(payload.get("review_lane") or "").strip() or None
    review_model = str(payload.get("review_model") or "").strip() or None

    try:
        meta = disp.dispatch(
            repo_path=Path(repo).expanduser(),
            title=title,
            body=body or title,
            path=DispatchPath(str(payload.get("path") or "full")),
            lane=lane,
            model_override=model,
            review_lane=review_lane,
            review_model=review_model,
        )
    except Exception as e:  # noqa: BLE001 - surface every dispatch failure to the UI
        return {"error": f"{type(e).__name__}: {e}"}
    return {
        "id": meta.id, "lane": meta.lane, "model": meta.model, "state": meta.state.value,
        "review": _review_label(meta),
    }


def _review_label(meta) -> str:
    """Short 'lane/model' (or 'default') describing a task's review slot."""
    if not meta.review_lane and not meta.review_model:
        return "default"
    lane = meta.review_lane or "claude"
    return f"{lane}/{meta.review_model}" if meta.review_model else lane


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
            elif u.path == "/api/repos":
                self._json(list_repos())
            elif u.path == "/api/issues":
                out = list_issues(q.get("repo", [""])[0])
                self._json(out, 400 if out.get("error") else 200)
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
            elif u.path == "/api/repos/add":
                out = add_repo(self._read_json().get("ref", ""))
                self._json(out, 400 if out.get("error") else 200)
            elif u.path == "/api/repos/trust":
                out = trust_repo(self._read_json().get("path", ""))
                self._json(out, 400 if out.get("error") else 200)
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

"""Deployer — the only container with docker.sock. Fixed command allowlist, shared-token auth.

Stdlib only. Config (JSON, deployer's own file, mounted read-only) maps repo name ->
{"cmd": [argv...], "cwd": "/abs/path"}. Request bodies are never read, so nothing a
caller sends can change what runs. Commands run as argv (no shell).

  POST /deploy/<repo>        start a deploy      -> 202 {"id", "repo", "status"}
  GET  /deploys/<id>/status  status              -> {"id","repo","status","exit_code"}
  GET  /deploys/<id>/log     stream log (chunked until the deploy finishes)
Auth: `Authorization: Bearer <DEPLOYER_TOKEN>` on every request.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


MAX_JOBS = 50
DEFAULT_SRC_ROOT = "/volume1/docker"


def _inside(root: str, path: str) -> bool:
    root, path = os.path.realpath(root), os.path.realpath(path)
    return path != root and os.path.commonpath([root, path]) == root


def load_allowlist(path: str, src_root: str = DEFAULT_SRC_ROOT) -> dict[str, dict]:
    if os.path.isdir(path):
        raise ValueError(f"allowlist path {path} is a directory (missing file bind-mounted?)")
    if not os.path.isfile(path):
        raise ValueError(f"allowlist file {path} not found")
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("allowlist must be a JSON object")
    out: dict[str, dict] = {}
    for name, ent in raw.items():
        cmd, cwd = ent.get("cmd"), ent.get("cwd")
        if not REPO_RE.match(name):
            raise ValueError(f"bad repo name {name!r}")
        if not (isinstance(cmd, list) and cmd and all(isinstance(a, str) for a in cmd)):
            raise ValueError(f"{name}: cmd must be a non-empty list of strings")
        if not (isinstance(cwd, str) and os.path.isabs(cwd)):
            raise ValueError(f"{name}: cwd must be an absolute path")
        if not _inside(src_root, cwd):
            raise ValueError(f"{name}: cwd {cwd} must be inside source root {src_root}")
        out[name] = {"cmd": list(cmd), "cwd": cwd}
    return out


class Deployer:
    def __init__(self, allowlist: dict[str, dict], token: str, log_dir: str | None = None):
        if not token:
            raise ValueError("DEPLOYER_TOKEN is empty; refusing to start")
        self.allowlist = allowlist
        self.token = token
        self.log_dir = log_dir or tempfile.mkdtemp(prefix="deployer-")
        os.makedirs(self.log_dir, exist_ok=True)
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}

    def auth_ok(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].encode(), self.token.encode())

    def start(self, repo: str) -> dict | None:
        """Job dict; {'busy': id} if one is already running; None if repo not allowlisted."""
        ent = self.allowlist.get(repo)
        if ent is None:
            return None
        with self.lock:
            for j in self.jobs.values():
                if j["repo"] == repo and j["status"] == "running":
                    return {"busy": j["id"]}
            jid = uuid.uuid4().hex
            job = {"id": jid, "repo": repo, "status": "running", "exit_code": None,
                   "log": os.path.join(self.log_dir, jid + ".log")}
            open(job["log"], "wb").close()
            self.jobs[jid] = job
            self._prune()
        threading.Thread(target=self._run, args=(job, ent), daemon=True).start()
        return job

    def _prune(self) -> None:
        """Keep at most MAX_JOBS jobs (oldest finished first), deleting pruned logs.

        Caller holds the lock.
        """
        for jid in list(self.jobs):
            if len(self.jobs) <= MAX_JOBS:
                break
            if self.jobs[jid]["status"] == "running":
                continue
            old = self.jobs.pop(jid)
            try:
                os.remove(old["log"])
            except OSError:
                pass

    def _run(self, job: dict, ent: dict) -> None:
        code = -1
        try:
            with open(job["log"], "ab") as log:
                try:
                    p = subprocess.Popen(ent["cmd"], cwd=ent["cwd"], stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT)
                    code = p.wait()
                except OSError as e:
                    log.write(f"deployer: failed to start: {e}\n".encode())
        finally:
            job["exit_code"] = code
            job["status"] = "succeeded" if code == 0 else "failed"

    @staticmethod
    def public(job: dict) -> dict:
        return {k: job[k] for k in ("id", "repo", "status", "exit_code")}


def make_handler(d: Deployer):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _gate(self) -> bool:
            if not d.auth_ok(self.headers.get("Authorization")):
                self.close_connection = True  # unread body must not be parsed as a request
                self._json(401, {"error": "unauthorized"})
                return False
            return True

        def do_POST(self):
            # Body deliberately never read; close so leftover bytes aren't parsed as a request.
            self.close_connection = True
            if not self._gate():
                return
            m = re.fullmatch(r"/deploy/([^/]+)", self.path)
            if not m:
                return self._json(404, {"error": "not found"})
            job = d.start(m.group(1))
            if job is None:
                return self._json(404, {"error": "unknown repo"})
            if "busy" in job:
                return self._json(409, {"error": "deploy already running", "id": job["busy"]})
            self._json(202, d.public(job))

        def do_GET(self):
            if not self._gate():
                return
            m = re.fullmatch(r"/deploys/([0-9a-f]{32})/(status|log)", self.path)
            job = d.jobs.get(m.group(1)) if m else None
            if not job:
                return self._json(404, {"error": "not found"})
            if m.group(2) == "status":
                return self._json(200, d.public(job))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                with open(job["log"], "rb") as f:
                    while True:
                        done = job["status"] != "running"
                        chunk = f.read(65536)
                        if chunk:
                            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                            self.wfile.flush()
                        elif done:
                            break
                        else:
                            time.sleep(0.2)
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    return H


def main() -> None:
    token = os.environ.get("DEPLOYER_TOKEN", "")
    cfg = os.environ.get("DEPLOYER_CONFIG", "/etc/deployer/allowlist.json")
    root = os.environ.get("DEPLOYER_SRC_ROOT", DEFAULT_SRC_ROOT)
    try:
        d = Deployer(load_allowlist(cfg, root), token, os.environ.get("DEPLOYER_LOG_DIR"))
    except (ValueError, OSError) as e:
        print(f"deployer: FATAL config error: {e}. See deploy/README.md (deployer setup).",
              flush=True)
        raise SystemExit(78) from e
    srv = ThreadingHTTPServer((os.environ.get("DEPLOYER_BIND", "0.0.0.0"),
                               int(os.environ.get("DEPLOYER_PORT", "8792"))), make_handler(d))
    srv.serve_forever()


if __name__ == "__main__":
    main()

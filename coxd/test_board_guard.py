"""Board Host/Origin guard — forged cross-site writes and DNS rebinding are refused.

Standalone (coxd has no pytest harness). Throwaway home:
    cd coxd && COXD_HOME=$(mktemp -d) ../.venv/bin/python test_board_guard.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-guard-")

import board  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

c = TestClient(board.app, base_url="http://localhost:8791")
fails = 0


def check(name: str, resp, want: int) -> None:
    global fails
    ok = resp.status_code == want
    fails += not ok
    print(("ok  " if ok else "FAIL"), name, resp.status_code)


# reads from our own hosts work
check("GET via localhost", c.get("/api/repos"), 200)
TRAEFIK = "coxd.home.manikantar.com"
check("GET via traefik name", c.get("/api/repos", headers={"host": TRAEFIK}), 200)
# DNS rebinding: attacker's hostname resolves to the NAS
check("GET with foreign Host", c.get("/api/repos", headers={"host": "evil.example"}), 403)
# CSRF: a page on another site POSTs a simple text/plain dispatch
check("POST from foreign Origin", c.post("/api/dispatch", content="{}",
      headers={"origin": "https://evil.example", "content-type": "text/plain"}), 403)
# our own board page posting (same origin) reaches the handler (400: no repo picked)
check("POST same-origin", c.post("/api/dispatch", json={},
      headers={"origin": "http://localhost:8791"}), 400)
check("POST via traefik origin", c.post("/api/dispatch", json={},
      headers={"host": TRAEFIK, "origin": f"https://{TRAEFIK}"}), 400)
# scripts (curl) send no Origin and still work
check("POST without Origin", c.post("/api/dispatch", json={}), 400)
# extra hosts via env
os.environ["COXD_ALLOWED_HOSTS"] = "nas.lan"
check("GET via COXD_ALLOWED_HOSTS", c.get("/api/repos", headers={"host": "nas.lan:8791"}), 200)

print("OK" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)

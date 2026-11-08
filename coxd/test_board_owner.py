"""Board owner login — writes need Remote-User + proxy secret, or the local admin token.

Standalone:  cd coxd && ../.venv/bin/python test_board_owner.py
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-owner-")
os.environ.pop("COXD_OWNER_USER", None)
os.environ.pop("COXD_PROXY_SECRET", None)

import board  # noqa: E402
import envscrub  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

c = TestClient(board.app, base_url="http://localhost:8791")
fails = 0


def check(name: str, ok: bool) -> None:
    global fails
    fails += not ok
    print(("ok  " if ok else "FAIL"), name)


def post(headers=None) -> int:
    return c.post("/api/dispatch", json={}, headers=headers or {}).status_code


SECRET, USER = "test-secret-xyz", "alice-example"
check("unset → unchanged (400 from handler)", post() == 400)
os.environ["COXD_OWNER_USER"] = USER
os.environ["COXD_PROXY_SECRET"] = SECRET
check("no user → 403", post() == 403)
check("wrong user → 403", post({"remote-user": "mallory", "x-coxd-proxy-secret": SECRET}) == 403)
check("user, no secret → 403", post({"remote-user": USER}) == 403)
check("user, wrong secret → 403", post({"remote-user": USER, "x-coxd-proxy-secret": "nope"}) == 403)
check("secret only → 403", post({"x-coxd-proxy-secret": SECRET}) == 403)
check("user + secret → allowed", post({"remote-user": USER, "x-coxd-proxy-secret": SECRET}) == 400)
check("GET unaffected", c.get("/api/repos").status_code == 200)
check("loopback, no token → 403", post() == 403)
tok = board.ensure_admin_token()
mode = stat.S_IMODE(os.stat(board.admin_token_path()).st_mode)
check("token file mode 600", mode == 0o600)
check("token stable across calls", board.ensure_admin_token() == tok)
check("admin token → allowed", post({"x-coxd-admin-token": tok}) == 400)
check("wrong admin token → 403", post({"x-coxd-admin-token": "bad"}) == 403)
del os.environ["COXD_PROXY_SECRET"]
check("secret unset fails closed", post({"remote-user": USER, "x-coxd-proxy-secret": ""}) == 403)
check("COXD_PROXY_SECRET in SECRET_VARS", "COXD_PROXY_SECRET" in envscrub.SECRET_VARS)

print("OK" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)

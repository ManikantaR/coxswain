import importlib.util
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

_p = Path(__file__).resolve().parent.parent / "deploy" / "deployer" / "server.py"
_spec = importlib.util.spec_from_file_location("deployer_server", _p)
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)

TOKEN = "tok-invented-123"


@pytest.fixture
def svc(tmp_path):
    work = tmp_path / "widgets"
    work.mkdir()
    allow = {"widgets": {"cmd": [sys.executable, "-c",
             "import os;print('deployed in', os.getcwd())"], "cwd": str(work)}}
    d = srv.Deployer(allow, TOKEN, str(tmp_path / "logs"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(d))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", work
    httpd.shutdown()


def call(url, method="GET", token=TOKEN, body=None):
    req = urllib.request.Request(url, method=method, data=body)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def wait_done(base, jid):
    for _ in range(200):
        _, b = call(f"{base}/deploys/{jid}/status")
        st = json.loads(b)
        if st["status"] != "running":
            return st
        time.sleep(0.05)
    raise AssertionError("timeout")


def test_unknown_repo_404(svc):
    assert call(svc[0] + "/deploy/nope", "POST")[0] == 404


def test_bad_token_401(svc):
    assert call(svc[0] + "/deploy/widgets", "POST", token="wrong")[0] == 401
    assert call(svc[0] + "/deploy/widgets", "POST", token=None)[0] == 401


def test_allowlisted_runs_and_streams(svc):
    base, work = svc
    code, b = call(base + "/deploy/widgets", "POST")
    assert code == 202
    jid = json.loads(b)["id"]
    st = wait_done(base, jid)
    assert st["status"] == "succeeded" and st["exit_code"] == 0
    code, log = call(f"{base}/deploys/{jid}/log")
    assert code == 200 and str(work).encode() in log


def test_body_cannot_change_command_or_cwd(svc, tmp_path):
    base, work = svc
    evil = json.dumps({"cmd": ["touch", str(tmp_path / "pwned")], "cwd": "/tmp"}).encode()
    code, b = call(base + "/deploy/widgets", "POST", body=evil)
    assert code == 202
    jid = json.loads(b)["id"]
    wait_done(base, jid)
    assert not (tmp_path / "pwned").exists()
    assert str(work).encode() in call(f"{base}/deploys/{jid}/log")[1]


def test_empty_token_refused():
    with pytest.raises(ValueError):
        srv.Deployer({}, "")

import importlib.util
import json
import subprocess
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


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t",
                           "-c", "user.email=t@example.invalid", *a],
                          capture_output=True, text=True, check=True).stdout.strip()


def head(work):
    return _git(work, "rev-parse", "HEAD")


@pytest.fixture
def svc(tmp_path):
    work = tmp_path / "widgets"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "commit", "-q", "--allow-empty", "-m", "init")
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


SHA = "0123456789abcdef0123456789abcdef01234567"


def test_unknown_repo_404(svc):
    assert call(svc[0] + "/deploy/nope/" + SHA, "POST")[0] == 404


def test_bad_token_401(svc):
    assert call(svc[0] + "/deploy/widgets/" + SHA, "POST", token="wrong")[0] == 401
    assert call(svc[0] + "/deploy/widgets/" + SHA, "POST", token=None)[0] == 401


def test_allowlisted_runs_and_streams(svc):
    base, work = svc
    code, b = call(base + "/deploy/widgets/" + head(work), "POST")
    assert code == 202
    jid = json.loads(b)["id"]
    st = wait_done(base, jid)
    assert st["status"] == "succeeded" and st["exit_code"] == 0
    code, log = call(f"{base}/deploys/{jid}/log")
    assert code == 200 and str(work).encode() in log


def test_body_cannot_change_command_or_cwd(svc, tmp_path):
    base, work = svc
    evil = json.dumps({"cmd": ["touch", str(tmp_path / "pwned")], "cwd": "/tmp"}).encode()
    code, b = call(base + "/deploy/widgets/" + SHA, "POST", body=evil)
    assert code == 202
    jid = json.loads(b)["id"]
    wait_done(base, jid)
    assert not (tmp_path / "pwned").exists()
    assert str(work).encode() in call(f"{base}/deploys/{jid}/log")[1]


def test_matching_head_succeeds_and_gets_expected_sha(tmp_path):
    work = tmp_path / "app"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "commit", "-q", "--allow-empty", "-m", "a")
    out = tmp_path / "seen"
    d = srv.Deployer({"app": {"cmd": [sys.executable, "-c",
                      f"import os;open({str(out)!r},'w').write(os.environ['EXPECTED_SHA'])"],
                      "cwd": str(work)}}, TOKEN, str(tmp_path / "logs"))
    sha = head(work)
    j = d.start("app", sha)
    for _ in range(200):
        if j["status"] != "running":
            break
        time.sleep(0.02)
    assert j["status"] == "succeeded" and j["reason"] is None
    assert out.read_text() == sha


def test_head_at_other_commit_is_stale_source(svc):
    base, work = svc
    old = head(work)
    _git(work, "commit", "-q", "--allow-empty", "-m", "newer")
    # command does nothing, HEAD != requested (older) sha
    code, b = call(base + "/deploy/widgets/" + old, "POST")
    assert code == 202
    st = wait_done(base, json.loads(b)["id"])
    assert st["status"] == "failed" and st["reason"] == "stale-source"
    assert st["exit_code"] != 0


def test_not_a_git_repo_is_stale_source(tmp_path):
    d = srv.Deployer({"w": {"cmd": ["true"], "cwd": str(tmp_path)}}, TOKEN, str(tmp_path / "l"))
    j = d.start("w", SHA)
    for _ in range(200):
        if j["status"] != "running":
            break
        time.sleep(0.02)
    assert j["status"] == "failed" and j["reason"] == "stale-source"


def test_bad_sha_400_and_body_ignored(svc, tmp_path):
    base, _ = svc
    evil = json.dumps({"sha": SHA, "cmd": ["touch", str(tmp_path / "pwned")]}).encode()
    for bad in ("abc", "g" * 40, SHA.upper(), SHA + "0", "..%2f..%2fetc"):
        assert call(f"{base}/deploy/widgets/{bad}", "POST", body=evil)[0] == 400
    assert call(base + "/deploy/widgets", "POST", body=evil)[0] == 404  # sha is required
    assert not (tmp_path / "pwned").exists()


def test_timeout_param_clamped_to_cap(tmp_path):
    d = srv.Deployer({"w": {"cmd": ["true"], "cwd": str(tmp_path)}}, TOKEN, str(tmp_path / "l"),
                     job_timeout_s=100)
    assert d.start("w", SHA, 5)["timeout_s"] == 5
    for _ in range(100):
        if not any(j["status"] == "running" for j in d.jobs.values()):
            break
        time.sleep(0.02)
    assert d.start("w", SHA, 9999)["timeout_s"] == 100


def test_empty_token_refused():
    with pytest.raises(ValueError):
        srv.Deployer({}, "")


def _write(tmp_path, cwd):
    f = tmp_path / "allow.json"
    f.write_text(json.dumps({"app": {"cmd": ["true"], "cwd": cwd}}))
    return str(f)


def test_cwd_must_be_inside_root(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    assert srv.load_allowlist(_write(tmp_path, str(root / "app")), str(root))
    for bad in (str(tmp_path / "other"), str(root), str(root / ".." / "x")):
        with pytest.raises(ValueError):
            srv.load_allowlist(_write(tmp_path, bad), str(root))


def test_missing_or_dir_allowlist_rejected(tmp_path):
    with pytest.raises(ValueError):
        srv.load_allowlist(str(tmp_path / "nope.json"), str(tmp_path))
    with pytest.raises(ValueError):
        srv.load_allowlist(str(tmp_path), str(tmp_path))


def test_job_history_capped_and_logs_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "MAX_JOBS", 3)
    d = srv.Deployer({"w": {"cmd": [sys.executable, "-c", "pass"], "cwd": str(tmp_path)}},
                     TOKEN, str(tmp_path / "logs"))
    for _ in range(6):
        j = d.start("w", SHA)
        for _ in range(200):
            if j["status"] != "running":
                break
            time.sleep(0.02)
    assert len(d.jobs) <= 3
    assert len(list((tmp_path / "logs").glob("*.log"))) <= 3

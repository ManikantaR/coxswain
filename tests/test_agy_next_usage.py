"""agy-next posts final usage once per published run; board errors warn and never fail the push."""
import threading
import time

from test_agy_next import run, run_args, setup, sh
from test_agy_next_status import STEP, fake_board, go

FULL_STREAM = (
    'echo \'{"event":"init"}\'\n'
    'echo hi > new.txt\n'
    'echo \'{"type":"result","duration_seconds":42,"usage":'
    '{"input_tokens":1200,"output_tokens":340,"thinking_tokens":160,"cache_read_tokens":500}}\'\n'
)

FALLBACK_STREAM = (
    'echo \'{"event":"init"}\'\n'
    'echo hi > new.txt\n'
    'echo \'{"step_update":{"step_type":"agent_response","state":"DONE",'
    '"usage":{"input_tokens":15,"output_tokens":25}}}\'\n'
)


def _usage_posts(posts):
    return [p for p in posts if p[0] != "/api/task/T1/agy-status"]


def test_usage_posted_once_on_publish(tmp_path):
    r, posts = go(tmp_path, STEP)
    assert r.returncode == 0, r.stderr
    up = _usage_posts(posts)
    assert len(up) == 1
    path, ctype, body = up[0]
    assert path == "/api/task/T1/agy-usage" and ctype == "application/json"
    assert set(body) == {
        "input_tokens", "output_tokens", "thinking_tokens",
        "cache_read_tokens", "duration_seconds", "model",
    }
    assert body["input_tokens"] == 3 and body["output_tokens"] == 4
    assert body["thinking_tokens"] == 0 and body["cache_read_tokens"] == 0
    assert isinstance(body["duration_seconds"], (int, float)) and body["duration_seconds"] >= 0
    assert body["model"] == "gemini-4-argon-high"


def test_usage_reads_full_result_and_custom_model(tmp_path):
    env, bare = setup(tmp_path, FULL_STREAM)
    srv, posts = fake_board((tmp_path / "web/api/task/T1/plan").read_text())
    env["COXD_URL"] = f"http://127.0.0.1:{srv.server_port}"
    try:
        r = run_args(env, "--model", "test-model-1", stdin="y\n")
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stderr
    up = _usage_posts(posts)
    assert len(up) == 1
    assert up[0][2] == {
        "input_tokens": 1200,
        "output_tokens": 340,
        "thinking_tokens": 160,
        "cache_read_tokens": 500,
        "duration_seconds": 42,
        "model": "test-model-1",
    }
    assert "coxd-agy/T1" in sh("git", "branch", cwd=bare)


def test_usage_fallback_without_result_event(tmp_path):
    r, posts = go(tmp_path, FALLBACK_STREAM)
    assert r.returncode == 0, r.stderr
    up = _usage_posts(posts)
    assert len(up) == 1
    body = up[0][2]
    assert body["input_tokens"] == 15 and body["output_tokens"] == 25
    assert body["thinking_tokens"] == 0 and body["cache_read_tokens"] == 0
    assert isinstance(body["duration_seconds"], (int, float)) and body["duration_seconds"] >= 0


def test_dead_board_warns_and_still_pushes(tmp_path):
    for i, mode in enumerate(("500", "drop", "hang")):
        sub = tmp_path / str(i)
        sub.mkdir()
        env, bare = setup(sub, STEP)
        srv, posts = fake_board((sub / "web/api/task/T1/plan").read_text(), mode)
        env["COXD_URL"] = f"http://127.0.0.1:{srv.server_port}"
        try:
            r = run(env, "y\n")
        finally:
            srv.shutdown()
        assert r.returncode == 0, (mode, r.stderr)
        assert "warning: could not post usage to board" in r.stderr, (mode, r.stderr)
        assert "coxd-agy/T1" in sh("git", "branch", cwd=bare), mode
        assert _usage_posts(posts), mode


def test_refused_connection_warns_and_still_pushes(tmp_path):
    flag = tmp_path / "ran"
    body = f"touch {flag}\n" + STEP
    env, bare = setup(tmp_path, body)
    srv, _ = fake_board((tmp_path / "web/api/task/T1/plan").read_text())
    env["COXD_URL"] = f"http://127.0.0.1:{srv.server_port}"

    def killer():
        for _ in range(100):
            if flag.exists():
                srv.shutdown()
                return
            time.sleep(0.02)

    t = threading.Thread(target=killer, daemon=True)
    t.start()
    try:
        r = run(env, "y\n")
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stderr
    assert "warning: could not post usage to board" in r.stderr
    assert "coxd-agy/T1" in sh("git", "branch", cwd=bare)


def test_no_usage_post_on_no_changes_or_declined_publish(tmp_path):
    sub1 = tmp_path / "nochanges"
    sub1.mkdir()
    r1, posts1 = go(sub1, "echo '{}'\n")
    assert r1.returncode == 1
    assert _usage_posts(posts1) == []

    sub2 = tmp_path / "declined"
    sub2.mkdir()
    r2, posts2 = go(sub2, STEP, stdin="n\n")
    assert r2.returncode == 1
    assert _usage_posts(posts2) == []

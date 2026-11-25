"""agy-next posts live status to a fake board; a dead board never fails the run."""
import http.server
import json
import threading
import time

from test_agy_next import run, setup

STEP = ('echo \'{"event":"init"}\'\n'
        'echo \'{"step_update":{"step_type":"tool","tool_name":"edit_file","state":"ACTIVE",'
        '"tool_info":{"parameters":{"Path":"web/x.ts"}}}}\'\n'
        'echo hi > new.txt\n'
        'echo \'{"type":"result","usage":{"input_tokens":3,"output_tokens":4}}\'\n')


def fake_board(plan_text, mode="ok"):
    posts = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = plan_text.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            posts.append((self.path, self.headers.get("Content-Type"),
                          json.loads(self.rfile.read(n))))
            if mode == "drop":      # accept, then hang up without answering
                self.close_connection = True
                return
            if mode == "hang":      # slower than curl's -m 2
                time.sleep(4)
                return
            self.send_response(500 if mode == "500" else 200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, posts


def go(tmp_path, agy_body, mode="ok", stdin="y\n"):
    env, bare = setup(tmp_path, agy_body)
    srv, posts = fake_board((tmp_path / "web/api/task/T1/plan").read_text(), mode)
    env["COXD_URL"] = f"http://127.0.0.1:{srv.server_port}"
    try:
        r = run(env, stdin)
    finally:
        srv.shutdown()
    return r, posts


def test_status_posted_to_board(tmp_path):
    r, posts = go(tmp_path, STEP)
    assert r.returncode == 0, r.stderr
    posts = [p for p in posts if p[0] == "/api/task/T1/agy-status"]
    assert posts and all(p[1] == "application/json" for p in posts)
    bodies = [p[2] for p in posts]
    assert any(b["action"] == "edit: web/x.ts" and b["step"] == 1 for b in bodies)
    assert bodies[-1]["action"] == "finished, publishing"
    assert bodies[-1]["tokens"] == 7 and bodies[-1]["step"] == 1
    assert all(set(b) == {"step", "action", "elapsed_s", "tokens"} for b in bodies)


def test_no_changes_status(tmp_path):
    r, posts = go(tmp_path, "echo '{}'\n")
    assert r.returncode == 1 and posts[-1][2]["action"] == "no changes"


def test_dead_board_does_not_fail_run(tmp_path):
    for i, mode in enumerate(("500", "drop", "hang")):
        sub = tmp_path / str(i)
        sub.mkdir()
        r, posts = go(sub, STEP, mode)
        assert r.returncode == 0, (mode, r.stderr)
        assert posts, mode  # it did try


def test_unreachable_board_does_not_fail_run(tmp_path):
    env, _ = setup(tmp_path, STEP)
    # plan comes from files, status goes to a closed port
    env["COXD_URL"] = f"file://{tmp_path}/web"
    assert run(env, "y\n").returncode == 0

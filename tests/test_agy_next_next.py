"""Tests for agy-next --next session driver, per-task locks, and rate-limit stop."""
import http.server
import json
import subprocess
import threading
from pathlib import Path

from test_agy_next import SCRIPT, setup, sh


def _setup_next(tmp_path, agy_body, queue_fn, plans=None, queue_status=200):
    env, bare = setup(tmp_path, agy_body)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    base = sh("git", "rev-parse", "HEAD", cwd=tmp_path / "repo/proj").strip()
    plans = plans or {}

    bin_ = tmp_path / "bin"
    sleep_log = tmp_path / "sleep.log"
    (bin_ / "sleep").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{sleep_log}"\n'
    )
    (bin_ / "sleep").chmod(0o755)

    agy_marker = tmp_path / "agy_calls.log"
    # Wrap agy so every invocation appends a line to agy_calls.log
    orig_agy = (bin_ / "agy").read_text()
    (bin_ / "agy").write_text(
        f'#!/bin/sh\nprintf "call\\n" >> "{agy_marker}"\n'
        + orig_agy.replace("#!/bin/sh\n", "", 1)
    )
    (bin_ / "agy").chmod(0o755)

    posts = []
    calls = {"queue": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/agy/queue":
                idx = calls["queue"]
                calls["queue"] += 1
                if queue_status != 200:
                    self.send_response(queue_status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                rows = queue_fn(idx) if callable(queue_fn) else queue_fn
                ready = sum(1 for r in rows if r.get("status") == "ready")
                body = json.dumps({
                    "queue": rows,
                    "ready": ready,
                    "blocked": len(rows) - ready,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path.startswith("/api/task/") and self.path.endswith("/plan"):
                tid = self.path.split("/")[3]
                p = plans.get(tid, {})
                body = json.dumps({
                    "task_id": tid,
                    "repo": "proj",
                    "branch": f"coxd-agy/{tid}",
                    "base_sha": base,
                    "round": p.get("round", 1),
                    "plan_md": p.get("plan_md", f"implement {tid}"),
                }).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path.startswith("/api/tasks"):
                body = json.dumps({"tasks": []}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            posts.append((self.path, json.loads(self.rfile.read(n))))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env["COXD_URL"] = f"http://127.0.0.1:{srv.server_port}"
    return env, bare, srv, agy_marker, sleep_log, posts


def _run_next(env, stdin="", *extra):
    return subprocess.run(
        [str(SCRIPT), "--next", *extra],
        env=env, input=stdin, capture_output=True, text=True,
    )


def _row(tid, status="ready", issue=10, title="Fix item", files=None,
         model="gemini-4-hydrogen-high", blocked_by=None):
    return {
        "id": tid,
        "repo": "proj",
        "issue": issue,
        "title": title,
        "files": files if files is not None else ["a.py", "b.py"],
        "status": status,
        "blocked_by": blocked_by if blocked_by is not None else [],
        "model": model,
        "cmd": f"agy-next {tid}" + (f" --model {model}" if model else ""),
    }


def test_picks_first_ready_skipping_blocked_and_sanitizes_title(tmp_path):
    rows = [
        _row("task-test-blocked", status="blocked", issue=5,
             title="Blocked first", blocked_by=["#1"]),
        _row("task-test-1", status="ready", issue=12,
             title="Add\x1b[31m\nwidget\tfeature", files=["a.py", "b.py", "c.py"],
             model="gemini-4-hydrogen-high"),
    ]
    env, _, srv, marker, _, _ = _setup_next(tmp_path, "echo hi > new.txt\n", rows)
    try:
        r = _run_next(env, stdin="n\n")
    finally:
        srv.shutdown()
    assert r.returncode == 0
    assert "#12 Add [31m widget feature · 3 files · gemini-4-hydrogen-high" in r.stdout
    assert "\x1b" not in r.stdout
    assert not marker.exists()


def test_null_issue_and_null_model_display(tmp_path):
    rows = [_row("task-test-1", status="ready", issue=None, title="Plain task",
                 files=["x.py"], model=None)]
    env, _, srv, marker, _, _ = _setup_next(tmp_path, "echo hi > new.txt\n", rows)
    try:
        r = _run_next(env, stdin="n\n")
    finally:
        srv.shutdown()
    assert r.returncode == 0
    assert "task-test-1 Plain task · 1 files · default" in r.stdout
    assert not marker.exists()


def test_asks_before_each_task_with_two_ready_tasks(tmp_path):
    def q(idx):
        if idx == 0:
            return [
                _row("task-test-1", issue=1, title="First task"),
                _row("task-test-2", issue=2, title="Second task"),
            ]
        return [_row("task-test-2", issue=2, title="Second task")]

    env, bare, srv, marker, _, _ = _setup_next(tmp_path, "echo hi > new.txt\n", q)
    try:
        # y (run 1), y (publish 1), y (run 2) -> EOF declines publish 2
        r = _run_next(env, stdin="y\ny\ny\n")
    finally:
        srv.shutdown()
    assert "Run it? [y/N]" in r.stdout
    assert "Run next task-test-2? [y/N]" in r.stdout
    assert len(marker.read_text().splitlines()) == 2
    branches = sh("git", "branch", cwd=bare)
    assert "coxd-agy/task-test-1" in branches
    assert "coxd-agy/task-test-2" not in branches


def test_declined_or_empty_stdin_runs_nothing(tmp_path):
    for idx, stdin in enumerate(("n\n", "")):
        sub = tmp_path / f"case-{idx}"
        sub.mkdir()
        rows = [_row("task-test-1", issue=1, title="First task")]
        env, _, srv, marker, _, _ = _setup_next(sub, "echo hi > new.txt\n", rows)
        try:
            r = _run_next(env, stdin=stdin)
        finally:
            srv.shutdown()
        assert r.returncode == 0
        assert "stopped" in r.stdout
        assert not marker.exists()


def test_y_to_run_but_n_to_publish_stops_and_does_not_push(tmp_path):
    rows = [
        _row("task-test-1", issue=1, title="First task"),
        _row("task-test-2", issue=2, title="Second task"),
    ]
    env, bare, srv, marker, sleep_log, _ = _setup_next(tmp_path, "echo hi > new.txt\n", rows)
    try:
        r = _run_next(env, stdin="y\nn\n")
    finally:
        srv.shutdown()
    assert r.returncode != 0
    assert "not published" in r.stdout
    assert "Run next" not in r.stdout
    assert "coxd-agy/task-test-1" not in sh("git", "branch", cwd=bare)
    assert len(marker.read_text().splitlines()) == 1
    assert not sleep_log.exists()


def test_stops_with_blocked_list_when_nothing_ready(tmp_path):
    rows = [
        _row("task-test-1", status="blocked", issue=21, title="Wait A", blocked_by=["#10"]),
        _row("task-test-2", status="blocked", issue=None, title="Wait B", blocked_by=["PR#7"]),
    ]
    env, _, srv, marker, _, _ = _setup_next(tmp_path, "echo hi > new.txt\n", rows)
    try:
        r = _run_next(env, stdin="y\n")
    finally:
        srv.shutdown()
    assert r.returncode == 0
    assert "#21 Wait A" in r.stdout and "#10" in r.stdout
    assert "task-test-2 Wait B" in r.stdout and "PR#7" in r.stdout
    assert not marker.exists()


def test_max_limits_clamp_and_validation(tmp_path):
    # AGY_NEXT_MAX=1 stops after 1
    sub1 = tmp_path / "m1"
    sub1.mkdir()
    rows = [_row(f"task-test-{i}", issue=i, title=f"Task {i}") for i in range(1, 7)]
    env1, _, srv1, marker1, sleep1, _ = _setup_next(
        sub1, "echo hi > new.txt\n", lambda idx: rows[idx:]
    )
    env1["AGY_NEXT_MAX"] = "1"
    try:
        r1 = _run_next(env1, stdin="y\ny\ny\ny\n")
    finally:
        srv1.shutdown()
    assert r1.returncode == 0
    assert "session limit reached (1)" in r1.stdout
    assert len(marker1.read_text().splitlines()) == 1
    assert not sleep1.exists()

    # AGY_NEXT_MAX=9 clamps to 5
    sub9 = tmp_path / "m9"
    sub9.mkdir()
    env9, _, srv9, marker9, _, _ = _setup_next(
        sub9, "echo hi > new.txt\n", lambda idx: rows[idx:]
    )
    env9["AGY_NEXT_MAX"] = "9"
    try:
        r9 = _run_next(env9, stdin="y\n" * 12)
    finally:
        srv9.shutdown()
    assert r9.returncode == 0
    assert "session limit reached (5)" in r9.stdout
    assert len(marker9.read_text().splitlines()) == 5

    # Invalid AGY_NEXT_MAX exits 2 without invoking agy
    for bad in ("abc", "0", "-2"):
        env_bad = dict(env1)
        env_bad["AGY_NEXT_MAX"] = bad
        rb = _run_next(env_bad, stdin="y\n")
        assert rb.returncode == 2


def test_pause_sleeps_90_between_tasks_only(tmp_path):
    rows = [_row(f"task-test-{i}", issue=i, title=f"Task {i}") for i in (1, 2)]
    env, _, srv, marker, sleep_log, _ = _setup_next(
        tmp_path, "echo hi > new.txt\n", lambda idx: rows[idx:]
    )
    env["AGY_NEXT_MAX"] = "2"
    try:
        r = _run_next(env, stdin="y\ny\ny\ny\n")
    finally:
        srv.shutdown()
    assert r.returncode == 0
    assert len(marker.read_text().splitlines()) == 2
    assert len(sleep_log.read_text().splitlines()) == 90

    # Declining the second task with N does not trigger another 90s pause
    sub_n = tmp_path / "declined-second"
    sub_n.mkdir()
    env_n, _, srv_n, marker_n, sleep_n, _ = _setup_next(
        sub_n, "echo hi > new.txt\n", lambda idx: rows[idx:]
    )
    env_n["AGY_NEXT_MAX"] = "3"
    try:
        rn = _run_next(env_n, stdin="y\ny\nn\n")
    finally:
        srv_n.shutdown()
    assert rn.returncode == 0
    assert len(marker_n.read_text().splitlines()) == 1
    assert len(sleep_n.read_text().splitlines()) == 90


def test_ai_tool_guard_blocks_next_but_not_single_task(tmp_path):
    rows = [_row("task-test-1", issue=1, title="First task")]
    env, _, srv, marker, _, _ = _setup_next(tmp_path, "echo hi > new.txt\n", rows)
    try:
        for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            bad_env = {**env, var: "1" if var == "CLAUDECODE" else "cli"}
            r = _run_next(bad_env, stdin="y\n")
            assert r.returncode == 2
            assert "plain terminal" in r.stderr
            assert not marker.exists()

        # Single-task invocation is unaffected by CLAUDECODE / CLAUDE_CODE_ENTRYPOINT
        ok_env = {**env, "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}
        r_single = subprocess.run(
            [str(SCRIPT), "T1"], env=ok_env, input="y\n",
            capture_output=True, text=True,
        )
        assert r_single.returncode == 0, r_single.stderr
    finally:
        srv.shutdown()


def test_per_task_lock_protects_and_cleans_up(tmp_path):
    env, _, srv, marker, _, _ = _setup_next(tmp_path, "echo hi > new.txt\n", [])
    runs = Path(env["AGY_NEXT_RUNS"])
    runs.mkdir(parents=True, exist_ok=True)
    lock = runs / ".lock-task-test-1"
    lock.mkdir()
    try:
        r_locked = subprocess.run(
            [str(SCRIPT), "task-test-1"], env=env, input="y\n",
            capture_output=True, text=True,
        )
        assert r_locked.returncode == 1
        assert "already running" in r_locked.stderr
        assert str(lock) in r_locked.stderr
        assert lock.exists()
        assert not marker.exists()

        # Remove stale lock -> normal run removes lock after completion
        lock.rmdir()
        r_ok = subprocess.run(
            [str(SCRIPT), "task-test-1"], env=env, input="y\n",
            capture_output=True, text=True,
        )
        assert r_ok.returncode == 0, r_ok.stderr
        assert not lock.exists()

        # Failed run (no diff -> exit 1) also removes lock
        (tmp_path / "bin/agy").write_text("#!/bin/sh\necho '{}'\n")
        (tmp_path / "bin/agy").chmod(0o755)
        r_fail = subprocess.run(
            [str(SCRIPT), "task-test-1"], env=env, input="y\n",
            capture_output=True, text=True,
        )
        assert r_fail.returncode == 1
        assert not lock.exists()
    finally:
        srv.shutdown()


def test_rate_limit_stops_session_and_ignores_14290_in_text(tmp_path):
    rows = [
        _row("task-test-1", issue=1, title="Task 1"),
        _row("task-test-2", issue=2, title="Task 2"),
    ]

    # Case 1: 429 on stderr (even after publish) stops without prompting for task 2
    sub1 = tmp_path / "rl-stderr"
    sub1.mkdir()
    env1, _, srv1, marker1, sleep1, _ = _setup_next(
        sub1,
        'echo "HTTP 429 Too Many Requests" >&2\necho hi > new.txt\n',
        lambda idx: rows[idx:],
    )
    try:
        r1 = _run_next(env1, stdin="y\ny\ny\ny\n")
    finally:
        srv1.shutdown()
    assert r1.returncode == 3
    assert "rate limit / quota" in r1.stderr
    assert "Run next" not in r1.stdout
    assert len(marker1.read_text().splitlines()) == 1
    assert not sleep1.exists()

    # Case 2: "rate limit" in error event in stream-json stops session
    sub2 = tmp_path / "rl-event"
    sub2.mkdir()
    env2, _, srv2, marker2, _, _ = _setup_next(
        sub2,
        'echo \'{"type":"error","error":{"message":"rate limit exceeded"}}\'\n',
        lambda idx: rows[idx:],
    )
    try:
        r2 = _run_next(env2, stdin="y\ny\n")
    finally:
        srv2.shutdown()
    assert r2.returncode == 3
    assert "rate limit / quota" in r2.stderr
    assert len(marker2.read_text().splitlines()) == 1

    # Case 3: 14290 and "quota" in non-error model/usage text do NOT trigger rate-limit stop
    sub3 = tmp_path / "rl-false-pos"
    sub3.mkdir()
    env3, _, srv3, marker3, _, _ = _setup_next(
        sub3,
        'echo hi > new.txt\n'
        'echo \'{"type":"assistant","text":"quota 14290"}\'\n'
        'echo \'{"type":"result","is_error":false,"usage":{"total_tokens":14290}}\'\n',
        lambda idx: rows[idx:],
    )
    env3["AGY_NEXT_MAX"] = "2"
    try:
        r3 = _run_next(env3, stdin="y\ny\ny\ny\n")
    finally:
        srv3.shutdown()
    assert r3.returncode == 0
    assert "session limit reached (2)" in r3.stdout
    assert len(marker3.read_text().splitlines()) == 2


def test_queue_fetch_failure_exits_1_no_agy(tmp_path):
    env, _, srv, marker, _, _ = _setup_next(
        tmp_path, "echo hi > new.txt\n", [], queue_status=500
    )
    try:
        r = _run_next(env, stdin="y\n")
    finally:
        srv.shutdown()
    assert r.returncode == 1
    assert not marker.exists()


def test_plans_only_skips_round_gt_1(tmp_path):
    rows = [_row("task-test-1", issue=1, title="Fix round task")]
    env, _, srv, marker, _, _ = _setup_next(
        tmp_path, "echo hi > new.txt\n", rows,
        plans={"task-test-1": {"round": 2}},
    )
    try:
        r = _run_next(env, stdin="y\ny\n")
    finally:
        srv.shutdown()
    assert r.returncode == 0
    assert "fix rounds are not sent to agy" in r.stdout
    assert not marker.exists()


def test_next_cli_usage_errors(tmp_path):
    env, _, srv, _, _, _ = _setup_next(tmp_path, "exit 0\n", [])
    try:
        for bad_args in (
            ["--next", "task-test-1"],
            ["--next", "--prune"],
            ["--next", "--model", "gemini-4-argon-high"],
            ["task-test-1", "--next"],
            ["--prune", "--next"],
        ):
            r = subprocess.run(
                [str(SCRIPT), *bad_args], env=env, capture_output=True, text=True
            )
            assert r.returncode == 2, bad_args
    finally:
        srv.shutdown()

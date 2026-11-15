import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools/human/agy-next"


def sh(*a, cwd=None, **kw):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True, **kw).stdout


def setup(tmp_path, agy_body):
    root = tmp_path / "repo"
    bare = tmp_path / "bare.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    sh("git", "init", "-q", "-b", "main", str(seed))
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    (seed / "a.txt").write_text("a\n")
    sh("git", "add", ".", cwd=seed)
    sh("git", "commit", "-qm", "init", cwd=seed, env=env)
    base = sh("git", "rev-parse", "HEAD", cwd=seed).strip()
    sh("git", "remote", "add", "origin", str(bare), cwd=seed)
    sh("git", "push", "-q", "origin", "main", cwd=seed)
    root.mkdir()
    sh("git", "clone", "-q", str(bare), str(root / "proj"))
    api = tmp_path / "web/api/task/T1"
    api.mkdir(parents=True)
    (api / "plan").write_text(json.dumps({
        "task_id": "T1", "repo": "proj", "branch": "coxd-agy/T1",
        "base_sha": base, "round": 2, "plan_md": "do it"}))
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "agy").write_text("#!/bin/sh\n" + agy_body)
    (bin_ / "agy").chmod(0o755)
    env.update(PATH=f"{bin_}:{os.environ['PATH']}", COXD_URL=f"file://{tmp_path}/web",
               AGY_REPO_ROOT=str(root), AGY_NEXT_LOG=str(tmp_path / "log/agy.log"),
               AGY_NEXT_RUNS=str(tmp_path / "runs"))
    return env, bare


def run(env, stdin=""):
    return subprocess.run([str(SCRIPT), "T1"], env=env, input=stdin,
                          capture_output=True, text=True)


def test_unset_url_exits_2(tmp_path):
    env, _ = setup(tmp_path, "exit 0\n")
    env.pop("COXD_URL")
    assert run(env).returncode == 2


def test_no_diff_exit_1_no_publish(tmp_path):
    env, bare = setup(tmp_path, 'echo \'{"status":"SUCCESS"}\'\n')
    r = run(env, "y\n")
    assert r.returncode == 1 and "agy made no changes" in r.stdout
    assert "coxd-agy/T1" not in sh("git", "branch", cwd=bare)


def test_diff_commits_and_publishes(tmp_path):
    env, bare = setup(
        tmp_path,
        'echo hi > new.txt\n'
        'echo \'{"type":"result","usage":{"input_tokens":3,"output_tokens":4}}\'\n')
    r = run(env, "y\n")
    assert r.returncode == 0, r.stderr
    subj = sh("git", "log", "-1", "--format=%s", "coxd-agy/T1", cwd=bare).strip()
    assert subj == "agy: T1 round 2"
    entry = json.loads((tmp_path / "log/agy.log").read_text().splitlines()[-1])
    assert entry["task_id"] == "T1" and entry["tokens"]["output_tokens"] == 4


def test_reused_worktree_no_new_changes_exits_1(tmp_path):
    env, bare = setup(tmp_path, "echo hi > new.txt\necho '{}'\n")
    assert run(env, "n\n").returncode == 1  # declined: commit stays local
    (tmp_path / "bin/agy").write_text("#!/bin/sh\necho '{}'\n")
    r = run(env, "y\n")
    assert r.returncode == 1 and "agy made no changes" in r.stdout
    assert "coxd-agy/T1" not in sh("git", "branch", cwd=bare)


def test_stale_worktree_fast_forwards_to_origin_branch(tmp_path):
    env, bare = setup(tmp_path, "echo '{}'\n")
    run(env)  # creates worktree at base; no changes -> exit 1
    seed = tmp_path / "seed"
    (seed / "b.txt").write_text("b\n")
    sh("git", "add", ".", cwd=seed)
    sh("git", "commit", "-qm", "coxd", cwd=seed, env=env)
    sh("git", "push", "-q", "origin", "HEAD:coxd-agy/T1", cwd=seed)
    run(env)
    assert (tmp_path / "repo/proj.agy/T1/b.txt").exists()


def test_diverged_worktree_refused(tmp_path):
    env, bare = setup(tmp_path, "echo hi > new.txt\necho '{}'\n")
    assert run(env, "n\n").returncode == 1  # local unpublished commit
    seed = tmp_path / "seed"
    (seed / "b.txt").write_text("b\n")
    sh("git", "add", ".", cwd=seed)
    sh("git", "commit", "-qm", "coxd", cwd=seed, env=env)
    sh("git", "push", "-q", "origin", "HEAD:coxd-agy/T1", cwd=seed)
    r = run(env, "y\n")
    assert r.returncode == 1 and "diverged" in r.stderr


def test_prompt_has_header_and_plan_and_streams(tmp_path):
    argv = tmp_path / "argv.txt"
    body = (f'printf "%s" "$2" > {argv}\necho hi > new.txt\n'
            'echo \'{"event":"init","init":{}}\'\n'
            'echo \'{"event":"step_update","step_update":{"step_type":"tool","state":"ACTIVE",'
            '"tool_name":"run_command","tool_info":"{\\"name\\": \\"run_command\\", '
            '\\"parameters\\": {\\"CommandLine\\": \\"pytest -q\\"}}"}}\'\n'
            'echo \'{"event":"step_update","step_update":{"step_type":"agent_response",'
            '"state":"DONE","usage":{"total_tokens":7}}}\'\n')
    env, _ = setup(tmp_path, body)
    r = run(env, "n\n")
    prompt = argv.read_text()
    assert "implementing a planned change" in prompt and "--- PLAN ---" in prompt
    assert prompt.rstrip().endswith("do it") and "fix round 2" in prompt
    assert "agy started" in r.stdout and "> run: pytest -q" in r.stdout
    assert "7 tokens so far" in r.stdout


def test_decline_does_not_publish(tmp_path):
    env, bare = setup(tmp_path, "echo hi > new.txt\necho '{}'\n")
    assert run(env, "n\n").returncode == 1
    assert "coxd-agy/T1" not in sh("git", "branch", cwd=bare)


def test_print_timeout_is_a_duration(tmp_path):
    # real agy rejects a bare number ("missing unit in duration") and exits 2
    body = ('case "$*" in *"--print-timeout 30m"*) ;; *) exit 2 ;; esac\n'
            'echo hi > new.txt\n')
    env, bare = setup(tmp_path, body)
    r = run(env, "y\n")
    assert r.returncode == 0, r.stderr


def run_args(env, *args, stdin=""):
    return subprocess.run([str(SCRIPT), "T1", *args], env=env, input=stdin,
                          capture_output=True, text=True)


def test_default_model_is_argon_high_and_logged(tmp_path):
    argv = tmp_path / "argv.txt"
    env, _ = setup(tmp_path, f'printf "%s\\n" "$@" > {argv}\necho hi > new.txt\n')
    env.pop("AGY_MODEL", None)
    assert run(env, "y\n").returncode == 0
    args = argv.read_text().splitlines()
    assert args[args.index("--model") + 1] == "gemini-4-argon-high"
    entry = json.loads((tmp_path / "log/agy.log").read_text().splitlines()[-1])
    assert entry["model"] == "gemini-4-argon-high"


def test_model_flag_overrides(tmp_path):
    argv = tmp_path / "argv.txt"
    env, _ = setup(tmp_path, f'printf "%s\\n" "$@" > {argv}\necho hi > new.txt\n')
    assert run_args(env, "--model", "claude-opus-5-5-high", stdin="y\n").returncode == 0
    args = argv.read_text().splitlines()
    assert args[args.index("--model") + 1] == "claude-opus-5-5-high"


def test_bad_model_rejected(tmp_path):
    env, _ = setup(tmp_path, "exit 0\n")
    assert run_args(env, "--model", "x; rm -rf /").returncode == 2
    assert run_args(env, "--bogus").returncode == 2


def test_sandbox_on_by_default_and_flag_disables(tmp_path):
    argv = tmp_path / "argv.txt"
    env, _ = setup(tmp_path, f'printf "%s\\n" "$@" > {argv}\necho hi > new.txt\n')
    assert run(env, "y\n").returncode == 0
    assert "--sandbox" in argv.read_text().splitlines()
    (tmp_path / "bin/agy").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {argv}\n')
    run_args(env, "--no-sandbox", stdin="n\n")
    assert "--sandbox" not in argv.read_text().splitlines()


def test_raw_stream_kept_after_run(tmp_path):
    env, _ = setup(tmp_path, 'echo \'{"event":"init","init":{}}\'\n')
    env["AGY_NEXT_RUNS"] = str(tmp_path / "runs")
    assert run(env).returncode == 1  # no changes
    kept = list((tmp_path / "runs").glob("T1-*.jsonl"))
    assert len(kept) == 1 and '"init"' in kept[0].read_text()


def test_session_settings_scope_files_to_worktree(tmp_path):
    out = tmp_path / "session.json"
    body = ('while [ $# -gt 0 ]; do [ "$1" = --settings ] && cp "$2" '
            + str(out) + '; shift; done\necho hi > new.txt\n')
    env, _ = setup(tmp_path, body)
    cfg = tmp_path / "agy-settings.json"
    cfg.write_text(json.dumps({"permissions": {"allow": ["command(git add)", "write_file(.)",
                                                         "read_file(/elsewhere)"],
                                               "deny": ["command(git push)"]}}))
    env["AGY_SETTINGS"] = str(cfg)
    assert run(env, "y\n").returncode == 0
    perms = json.loads(out.read_text())["permissions"]
    wt = str(tmp_path / "repo/proj.agy/T1")
    assert f"write_file({wt})" in perms["allow"] and f"read_file({wt})" in perms["allow"]
    assert "write_file(.)" not in perms["allow"] and "read_file(/elsewhere)" not in perms["allow"]
    assert "command(git add)" in perms["allow"] and perms["deny"] == ["command(git push)"]


def test_header_forbids_leaving_worktree(tmp_path):
    argv = tmp_path / "argv.txt"
    env, _ = setup(tmp_path, f'printf "%s" "$2" > {argv}\n')
    run(env)
    p = argv.read_text()
    assert "never ask to bypass the sandbox" in p and "do not search for one elsewhere" in p

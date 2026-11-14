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
               AGY_REPO_ROOT=str(root), AGY_NEXT_LOG=str(tmp_path / "log/agy.log"))
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
        'echo hi > new.txt\necho \'{"usage":{"input_tokens":3,"output_tokens":4}}\'\n')
    r = run(env, "y\n")
    assert r.returncode == 0, r.stderr
    subj = sh("git", "log", "-1", "--format=%s", "coxd-agy/T1", cwd=bare).strip()
    assert subj == "agy: T1 round 2"
    entry = json.loads((tmp_path / "log/agy.log").read_text().splitlines()[-1])
    assert entry["task_id"] == "T1" and entry["tokens"]["output_tokens"] == 4


def test_decline_does_not_publish(tmp_path):
    env, bare = setup(tmp_path, "echo hi > new.txt\necho '{}'\n")
    assert run(env, "n\n").returncode == 1
    assert "coxd-agy/T1" not in sh("git", "branch", cwd=bare)

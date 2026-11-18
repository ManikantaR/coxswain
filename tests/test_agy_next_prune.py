import json
import shutil
import subprocess
from pathlib import Path

from test_agy_next import setup, sh

SCRIPT = Path(__file__).resolve().parent.parent / "tools/human/agy-next"

CURL_STUB = """#!/usr/bin/env python3
import os
import shutil
import sys

args = sys.argv[1:]
if "-X" in args or "POST" in args:
    sys.exit(0)
out = None
url = None
i = 0
while i < len(args):
    a = args[i]
    if a == "-o" and i + 1 < len(args):
        out = args[i + 1]
        i += 2
        continue
    if a in ("-m", "-H", "--data-binary"):
        i += 2
        continue
    if not a.startswith("-"):
        url = a
    i += 1
if not url or not url.startswith("file://"):
    sys.exit(1)
path = url[len("file://"):].split("?", 1)[0]
if not os.path.isfile(path):
    sys.stderr.write(f"curl: (22) missing {path}\\n")
    sys.exit(22)
if out:
    shutil.copyfile(path, out)
else:
    sys.stdout.write(open(path).read())
"""

GH_STUB = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
log_path = os.environ.get("GH_LOG")
if log_path:
    with open(log_path, "a") as fh:
        fh.write(json.dumps(args) + "\\n")

if os.environ.get("GH_FAIL") == "1":
    sys.stderr.write("gh: simulated failure\\n")
    sys.exit(1)

head = ""
if "--head" in args:
    head = args[args.index("--head") + 1]
branch_key = head.replace("/", "_")
fixture_dir = Path(os.environ["GH_FIXTURES"])
f = fixture_dir / f"{branch_key}.json"
if f.exists():
    sys.stdout.write(f.read_text())
else:
    sys.stdout.write("[]\\n")
"""

PS_STUB = """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

f = Path(os.environ["PS_FIXTURE"])
if f.exists():
    sys.stdout.write(f.read_text())
"""


def setup_prune(tmp_path, agy_body="echo hi > new.txt\n"):
    env, bare = setup(tmp_path, agy_body)
    bin_ = tmp_path / "bin"

    curl_p = bin_ / "curl"
    curl_p.write_text(CURL_STUB)
    curl_p.chmod(0o755)

    gh_dir = tmp_path / "gh_fixtures"
    gh_dir.mkdir()
    gh_p = bin_ / "gh"
    gh_p.write_text(GH_STUB)
    gh_p.chmod(0o755)

    ps_file = tmp_path / "ps.txt"
    ps_file.write_text("")
    ps_p = bin_ / "ps"
    ps_p.write_text(PS_STUB)
    ps_p.chmod(0o755)

    tasks_file = tmp_path / "web/api/tasks"
    tasks_file.write_text(json.dumps({"tasks": []}))

    env.update(
        GH_FIXTURES=str(gh_dir),
        GH_LOG=str(tmp_path / "gh_calls.jsonl"),
        PS_FIXTURE=str(ps_file),
    )
    return env, bare, tmp_path


def add_worktree(tmp_path, env, tid, repo="proj", subdir=None, push_origin=False):
    main = tmp_path / "repo" / repo
    parent = tmp_path / "repo" / (subdir or f"{repo}.agy")
    parent.mkdir(parents=True, exist_ok=True)
    wt = parent / tid
    branch = f"coxd-agy/{tid}"
    sh("git", "-C", str(main), "worktree", "add", "-q", "-b", branch, str(wt), env=env)
    if push_origin:
        sh("git", "-C", str(main), "push", "-q", "origin", branch, env=env)
    return wt, branch


def set_gh_pr(tmp_path, branch, prs):
    key = branch.replace("/", "_")
    (tmp_path / "gh_fixtures" / f"{key}.json").write_text(json.dumps(prs))


def set_board_tasks(tmp_path, tasks):
    (tmp_path / "web/api/tasks").write_text(json.dumps({"tasks": tasks}))


def run_prune(env, *extra):
    return subprocess.run([str(SCRIPT), "--prune", *extra], env=env, capture_output=True, text=True)


def test_merged_clean_removed(tmp_path):
    env, _, root = setup_prune(tmp_path)
    wt, branch = add_worktree(root, env, "T1")
    set_gh_pr(root, branch, [{"number": 12, "state": "MERGED"}])

    r = run_prune(env)
    assert r.returncode == 0, r.stderr
    assert "removed 1, kept 0" in r.stdout
    assert not wt.exists()
    branches = sh("git", "-C", str(root / "repo/proj"), "branch")
    assert branch not in branches
    calls = [json.loads(x) for x in (root / "gh_calls.jsonl").read_text().splitlines()]
    assert ["--head", branch, "--state", "all", "--json", "state,number,headRefOid,isCrossRepository"] == calls[0][-6:]


def test_merged_dirty_kept(tmp_path):
    env, _, root = setup_prune(tmp_path)
    wt1, b1 = add_worktree(root, env, "T1")
    (wt1 / "untracked.txt").write_text("dirty\n")
    set_gh_pr(root, b1, [{"number": 1, "state": "MERGED"}])

    wt2, b2 = add_worktree(root, env, "T2")
    (wt2 / "a.txt").write_text("modified\n")
    set_gh_pr(root, b2, [{"number": 2, "state": "MERGED"}])

    r = run_prune(env)
    assert r.returncode == 0, r.stderr
    assert wt1.exists() and wt2.exists()
    assert "removed 0, kept 2" in r.stdout
    assert "T1: uncommitted" in r.stdout and "T2: uncommitted" in r.stdout


def test_open_pr_not_on_board_kept(tmp_path):
    env, _, root = setup_prune(tmp_path)
    wt, branch = add_worktree(root, env, "T1")
    set_gh_pr(root, branch, [{"number": 5, "state": "OPEN"}])

    r = run_prune(env)
    assert r.returncode == 0, r.stderr
    assert wt.exists()
    assert "removed 0, kept 1 (T1: pr open/unknown)" in r.stdout


def test_no_pr_info_and_gh_failure_kept(tmp_path):
    env, _, root = setup_prune(tmp_path)
    wt, _ = add_worktree(root, env, "T1")

    r_empty = run_prune(env)
    assert r_empty.returncode == 0
    assert wt.exists()
    assert "T1: pr open/unknown" in r_empty.stdout

    env_fail = {**env, "GH_FAIL": "1"}
    r_fail = run_prune(env_fail)
    assert r_fail.returncode == 0
    assert wt.exists()
    assert "T1: pr open/unknown" in r_fail.stdout


def test_active_run_kept_with_word_boundary(tmp_path):
    env, _, root = setup_prune(tmp_path)
    wt1, b1 = add_worktree(root, env, "T1")
    wt2, b2 = add_worktree(root, env, "T2")
    set_gh_pr(root, b1, [{"number": 1, "state": "MERGED"}])
    set_gh_pr(root, b2, [{"number": 2, "state": "MERGED"}])

    ps_file = root / "ps.txt"
    # T10 must NOT protect T1; path match protects T2
    ps_file.write_text(
        "101 agy-next T10\n"
        f"102 agy --settings {root}/repo/proj.agy/T2/session.json\n"
    )
    r = run_prune(env)
    assert r.returncode == 0, r.stderr
    assert not wt1.exists()
    assert wt2.exists()
    assert "removed 1, kept 1 (T2: active run)" in r.stdout

    # Direct agy-next T3 token also protects T3
    wt3, b3 = add_worktree(root, env, "T3")
    set_gh_pr(root, b3, [{"number": 3, "state": "MERGED"}])
    ps_file.write_text("103 /usr/local/bin/agy-next T3 --no-sandbox\n")
    r2 = run_prune(env)
    assert r2.returncode == 0
    assert wt3.exists()
    assert "T3: active run" in r2.stdout


def test_board_landed_or_archived_without_gh_removed(tmp_path):
    env, _, root = setup_prune(tmp_path)
    wt1, _ = add_worktree(root, env, "T1")
    wt2, _ = add_worktree(root, env, "T2")
    env["GH_FAIL"] = "1"
    set_board_tasks(root, [
        {"id": "T1", "state": "landed", "archived": False},
        {"id": "T2", "state": "done", "archived": True},
    ])

    r = run_prune(env)
    assert r.returncode == 0, r.stderr
    assert not wt1.exists() and not wt2.exists()
    assert "removed 2, kept 0" in r.stdout


def test_dry_run_leaves_everything_unchanged(tmp_path):
    env, bare, root = setup_prune(tmp_path)
    wt, branch = add_worktree(root, env, "T1", push_origin=True)
    head = sh("git", "-C", str(wt), "rev-parse", "HEAD", env=env).strip()
    set_gh_pr(root, branch, [{"number": 9, "state": "MERGED", "headRefOid": head}])

    r = run_prune(env, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "would remove" in r.stdout
    assert "would remove 1, kept 0" in r.stdout
    assert f"would delete remote {branch}" in r.stdout
    assert wt.exists()
    assert branch in sh("git", "-C", str(root / "repo/proj"), "branch")
    assert branch in sh("git", "branch", cwd=bare)


def test_remote_branch_deleted_once(tmp_path):
    env, bare, root = setup_prune(tmp_path)
    real_git = shutil.which("git")
    git_log = root / "git_calls.log"
    git_wrap = root / "bin/git"
    git_wrap.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{git_log}"\nexec "{real_git}" "$@"\n'
    )
    git_wrap.chmod(0o755)

    wt, branch = add_worktree(root, env, "T1", push_origin=True)
    head = sh("git", "-C", str(wt), "rev-parse", "HEAD", env=env).strip()
    set_gh_pr(root, branch, [{"number": 11, "state": "MERGED", "headRefOid": head}])
    git_log.write_text("")

    r1 = run_prune(env)
    assert r1.returncode == 0, r1.stderr
    assert f"deleting remote {branch}" in r1.stdout
    assert not wt.exists()
    assert branch not in sh("git", "branch", cwd=bare)
    push_deletes_1 = [line for line in git_log.read_text().splitlines() if "--delete" in line]
    assert len(push_deletes_1) == 1

    git_log.write_text("")
    r2 = run_prune(env)
    assert r2.returncode == 0, r2.stderr
    assert "deleting" not in r2.stdout
    push_deletes_2 = [line for line in git_log.read_text().splitlines() if "--delete" in line]
    assert push_deletes_2 == []


def test_outside_agy_dir_untouched(tmp_path):
    env, _, root = setup_prune(tmp_path)
    wt_other, branch = add_worktree(root, env, "x", subdir="proj.other")
    set_gh_pr(root, branch, [{"number": 20, "state": "MERGED"}])
    set_board_tasks(root, [{"id": "x", "state": "landed", "archived": True}])

    r = run_prune(env)
    assert r.returncode == 0, r.stderr
    assert wt_other.exists()
    assert branch in sh("git", "-C", str(root / "repo/proj"), "branch")


def test_normal_run_auto_prunes_quietly_and_creates_own(tmp_path):
    env, _, root = setup_prune(tmp_path)
    base = sh("git", "-C", str(root / "repo/proj"), "rev-parse", "HEAD").strip()
    t2_api = root / "web/api/task/T2"
    t2_api.mkdir(parents=True)
    (t2_api / "plan").write_text(json.dumps({
        "task_id": "T2", "repo": "proj", "branch": "coxd-agy/T2",
        "base_sha": base, "round": 1, "plan_md": "do t2"}))

    # T1 is merged & clean -> auto-pruned; T2 already exists & is merged -> excluded as current tid
    wt1, b1 = add_worktree(root, env, "T1")
    set_gh_pr(root, b1, [{"number": 1, "state": "MERGED"}])
    wt2, b2 = add_worktree(root, env, "T2")
    set_gh_pr(root, b2, [{"number": 2, "state": "MERGED"}])

    r = subprocess.run([str(SCRIPT), "T2"], env=env, input="y\n", capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert not wt1.exists()
    assert wt2.exists()
    assert "agy-next prune: removed 1, kept 0" in r.stdout

    # When nothing is prunable, quiet mode emits no prune summary
    r2 = subprocess.run([str(SCRIPT), "T2"], env=env, input="y\n", capture_output=True, text=True)
    assert "agy-next prune:" not in r2.stdout


def test_auto_prune_opt_out(tmp_path):
    env, _, root = setup_prune(tmp_path)
    base = sh("git", "-C", str(root / "repo/proj"), "rev-parse", "HEAD").strip()
    t2_api = root / "web/api/task/T2"
    t2_api.mkdir(parents=True)
    (t2_api / "plan").write_text(json.dumps({
        "task_id": "T2", "repo": "proj", "branch": "coxd-agy/T2",
        "base_sha": base, "round": 1, "plan_md": "do t2"}))
    wt1, b1 = add_worktree(root, env, "T1")
    set_gh_pr(root, b1, [{"number": 1, "state": "MERGED"}])

    env["AGY_NEXT_PRUNE"] = "0"
    r = subprocess.run([str(SCRIPT), "T2"], env=env, input="y\n", capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert wt1.exists()
    assert (root / "repo/proj.agy/T2").exists()


def test_prune_cli_validation_and_skips_grants(tmp_path):
    env, _, root = setup_prune(tmp_path)
    assert subprocess.run([str(SCRIPT), "--dry-run"], env=env,
                          capture_output=True, text=True).returncode == 2
    assert subprocess.run([str(SCRIPT), "T1", "--dry-run"], env=env,
                          capture_output=True, text=True).returncode == 2
    assert run_prune(env, "--bogus").returncode == 2

    env_no_url = {k: v for k, v in env.items() if k != "COXD_URL"}
    assert run_prune(env_no_url).returncode == 2

    # Prune skips the Antigravity grants check
    cfg = root / "app.json"
    cfg.write_text(json.dumps({"userSettings": {"globalPermissionGrants": {"allow": ["x"]}}}))
    env["AGY_APP_CONFIG"] = str(cfg)
    assert run_prune(env).returncode == 0


def commit_in(wt, env, name="extra.txt"):
    (wt / name).write_text("work\n")
    sh("git", "-C", str(wt), "add", name, env=env)
    sh("git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "wip", env=env)
    return sh("git", "-C", str(wt), "rev-parse", "HEAD", env=env).strip()


def test_unpushed_commits_kept(tmp_path):
    env, _, root = setup_prune(tmp_path)
    wt, branch = add_worktree(root, env, "T1")
    commit_in(wt, env)
    set_gh_pr(root, branch, [{"number": 1, "state": "MERGED", "headRefOid": "0" * 40}])
    set_board_tasks(root, [{"id": "T1", "state": "landed", "archived": True}])

    r = run_prune(env)
    assert r.returncode == 0, r.stderr
    assert wt.exists()
    assert "T1: unpushed commits" in r.stdout
    assert branch in sh("git", "-C", str(root / "repo/proj"), "branch")


def test_commits_after_merged_head_kept_but_merged_head_removed(tmp_path):
    env, _, root = setup_prune(tmp_path)
    wt1, b1 = add_worktree(root, env, "T1")
    merged = commit_in(wt1, env)  # head of the merged PR (not on any remote ref: squash-merged)
    set_gh_pr(root, b1, [{"number": 1, "state": "MERGED", "headRefOid": merged}])
    wt2, b2 = add_worktree(root, env, "T2")
    m2 = commit_in(wt2, env)
    commit_in(wt2, env, "round2.txt")  # round-2 work after the merge
    set_gh_pr(root, b2, [{"number": 2, "state": "MERGED", "headRefOid": m2}])

    r = run_prune(env)
    assert r.returncode == 0, r.stderr
    assert not wt1.exists()
    assert wt2.exists()
    assert "T2: unpushed commits" in r.stdout


def test_fork_pr_with_same_branch_name_ignored(tmp_path):
    env, _, root = setup_prune(tmp_path)
    wt, branch = add_worktree(root, env, "T1")
    set_gh_pr(root, branch, [{"number": 1, "state": "MERGED", "isCrossRepository": True}])

    r = run_prune(env)
    assert r.returncode == 0, r.stderr
    assert wt.exists()
    assert "T1: pr open/unknown" in r.stdout

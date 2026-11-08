"""The smoke-sandbox mount check, fed synthetic mountinfo samples."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "smoke-sandbox.sh"
H = "a" * 64
V = "@docker/volumes"
GOOD = f"""\
1 0 0:1 / / rw - overlay overlay rw
2 1 0:2 / /proc rw - proc proc rw
3 1 0:3 / /dev rw - tmpfs tmpfs rw
4 1 0:4 /{V}/deploy_coxd-work/_data /work rw - btrfs /dev/x rw
5 1 0:4 /{V}/deploy_coxd-repos/_data /repo rw - btrfs /dev/x rw
6 1 0:4 /{V}/deploy_coxd-data/_data /data/coxswain rw - btrfs /dev/x rw
7 1 0:4 /{V}/deploy_claude-creds/_data /home/coxw/.claude rw - btrfs /dev/x rw
8 1 0:4 /@docker/containers/{H}/resolv.conf /etc/resolv.conf rw - btrfs /dev/x rw
9 1 0:4 /@docker/containers/{H}/hostname /etc/hostname rw - btrfs /dev/x rw
10 1 0:4 /@docker/containers/{H}/hosts /etc/hosts rw - btrfs /dev/x rw
"""


def run(tmp_path, text):
    f = tmp_path / "mountinfo"
    f.write_text(text)
    env = {**os.environ, "COXD_WORKER_USER": "coxw", "SMOKE_MOUNTS_ONLY": "1",
           "SMOKE_MOUNTINFO": str(f), "COXD_HOME": "/data/coxswain"}
    return subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)


def test_expected_mounts_pass(tmp_path):
    r = run(tmp_path, GOOD)
    assert r.returncode == 0, r.stdout
    assert "ok   no document/data volume mounted" in r.stdout
    assert "ALL OK" in r.stdout


def test_share_bound_onto_repo_fails(tmp_path):
    r = run(tmp_path, GOOD + "11 1 0:5 /documents /repo rw - btrfs /dev/y rw\n")
    assert r.returncode == 1
    assert "FAIL mount not backed by a docker volume: /repo" in r.stdout

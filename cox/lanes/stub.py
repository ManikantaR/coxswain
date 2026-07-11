"""Deterministic stub lane for tokenless e2e tests (T-13).

The "agent" is a tiny local script that reads the brief, makes a real commit +
evidence in the worktree, and appends `done:` to the status log — no network,
no tokens. This is what lets the whole dispatch->gate->ship->merge loop be
proven in CI (DESIGN §3).
"""

from __future__ import annotations

from pathlib import Path

from .. import proc
from ..model import CostEntry, ModelSpec
from .base import RunResult, SpawnHandle

# A self-contained worker: touch a file, add evidence, commit, log done.
_WORKER = r"""
import sys, subprocess, pathlib, datetime, json
wt = pathlib.Path.cwd()
status = pathlib.Path(sys.argv[1])
evidence = pathlib.Path(sys.argv[2])
evidence.mkdir(parents=True, exist_ok=True)
(wt / "STUB_CHANGE.txt").write_text("stub change %s\n" % datetime.datetime.now().isoformat())
(evidence / "test-output.txt").write_text("1 passed\n")
# self-verify against any acceptance criteria (P2)
acc = status.parent / "acceptance.json"
if acc.exists():
    items = json.loads(acc.read_text())
    (evidence / "selfcheck.json").write_text(json.dumps(
        [{"item": c, "ok": True, "note": "stub verified"} for c in items]))
subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
subprocess.run(["git", "-c", "user.email=stub@cox", "-c", "user.name=stub",
                "commit", "-q", "-m", "stub: implement brief"], cwd=wt, check=True)
with open(status, "a") as f:
    f.write("done: stub implemented brief\n")
"""


class StubLane:
    name = "stub"

    def spawn(
        self, brief_path: Path, worktree: Path, model: ModelSpec, log_path: Path, pid_path: Path
    ) -> SpawnHandle:
        status = brief_path.parent / "status.log"
        evidence = brief_path.parent / "evidence"
        script = brief_path.parent / "_stub_worker.py"
        script.write_text(_WORKER, encoding="utf-8")
        argv = ["python3", str(script), str(status), str(evidence)]
        pid = proc.spawn_detached(argv, log_path=log_path, pid_path=pid_path, cwd=worktree)
        return SpawnHandle(pid=pid, log_path=log_path)

    def resume(
        self, session_id: str, feedback: str, worktree: Path, log_path: Path, pid_path: Path
    ) -> SpawnHandle:
        # A fix round just re-commits; the stub always "fixes" successfully.
        argv = [
            "python3",
            "-c",
            "import subprocess,pathlib;p=pathlib.Path.cwd();"
            "(p/'STUB_FIX.txt').write_text('fixed');"
            "subprocess.run(['git','add','-A'],cwd=p,check=True);"
            "subprocess.run(['git','-c','user.email=s@c','-c','user.name=s',"
            "'commit','-q','-m','stub: fix'],cwd=p,check=True)",
        ]
        pid = proc.spawn_detached(argv, log_path=log_path, pid_path=pid_path, cwd=worktree)
        return SpawnHandle(pid=pid, log_path=log_path)

    def parse_result(self, log_path: Path, phase: str) -> RunResult:
        return RunResult(
            outcome="success",
            session_id="stub-session",
            cost=CostEntry(phase=phase, tokens_in=0, tokens_out=0, cost_usd=0.0),
        )


def canned_review(verdict: str = "approve") -> dict:
    """A fixed review.json for the e2e test (no review model call)."""
    return {"findings": [], "verdict": verdict}

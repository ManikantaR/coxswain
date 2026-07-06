"""Deterministic gate steps 1-4 (T-09, DESIGN §2.4).

rebase -> test -> lint -> evidence. All zero-token. A red baseline routes
straight back to `fixing` with the failing output as feedback — the review
pass (step 5, review.py) NEVER runs on code that fails its own tests, so we
never pay for judgment on a red baseline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import evidence, proc, store
from .repoconfig import RepoConfig, load_repo_config


@dataclass
class GateReport:
    task_id: str
    passed: bool
    failing_step: str | None = None
    steps: dict[str, str] = field(default_factory=dict)  # step -> ok|red|skip
    feedback: str = ""

    def to_json(self) -> dict:
        return {
            "task_id": self.task_id,
            "passed": self.passed,
            "failing_step": self.failing_step,
            "steps": self.steps,
        }


def _tail(text: str, n: int = 200) -> str:
    return "\n".join(text.splitlines()[-n:])


def ingest_worker_result(task_id: str) -> None:
    """Record the implement worker's cost + capture its session_id, once.

    Nothing else reads the worker.log for the implement phase: dispatch spawns
    before a session_id exists, so without this the cost ledger stays $0 and
    fix-round --resume has no session to resume (shakedown BUG-03). Idempotent —
    guarded on an existing implement-phase cost entry — so re-gating after a fix
    round does not double-count. The fix round records its own cost in fix.py.
    """
    from dataclasses import replace

    from .lanes import get_lane

    meta = store.load_meta(task_id)
    if any(c.phase == "implement" for c in store.read_cost(task_id)):
        return  # already ingested
    log_path = store.task_data_dir(task_id) / "worker.log"
    res = get_lane(meta.lane).parse_result(log_path, phase="implement")
    if res.cost is not None:
        store.append_cost(task_id, res.cost)
    if res.session_id and not meta.session_id:
        store.save_meta(replace(meta, session_id=res.session_id))


def run_gate(task_id: str) -> GateReport:
    ingest_worker_result(task_id)
    meta = store.load_meta(task_id)
    worktree = Path(meta.worktree)
    cfg = load_repo_config(worktree)
    report = GateReport(task_id=task_id, passed=True)

    # 1. rebase onto target
    try:
        proc.run(["git", "fetch", "--quiet"], cwd=worktree, ok_rc=(0, 1, 128))
        r = proc.run(
            ["git", "rebase", f"origin/{cfg.target_branch}"], cwd=worktree, ok_rc=(0, 1, 128)
        )
        if r.rc != 0:
            proc.run(["git", "rebase", "--abort"], cwd=worktree, ok_rc=(0, 1, 128))
            return _fail(report, "rebase", f"rebase onto {cfg.target_branch} conflicted:\n{r.out}")
        report.steps["rebase"] = "ok"
    except proc.BosunProcError as e:
        return _fail(report, "rebase", str(e))

    # 2. test  3. lint  (configured commands are the strong, tokenless path)
    for step, cmd in (("test", cfg.test_cmd), ("lint", cfg.lint_cmd)):
        if not cmd:
            report.steps[step] = "skip"
            continue
        try:
            proc.run(_shell(cmd), cwd=worktree)
            report.steps[step] = "ok"
        except proc.BosunProcError as e:
            return _fail(report, step, f"{step} failed:\n{_tail(e.out + e.err)}")

    # 4. evidence
    ev = evidence.check(task_id, meta.dispatched_at)
    report.steps["evidence"] = "ok" if ev.ok else "red"
    if not ev.ok:
        return _fail(report, "evidence", ev.note, evidence_missing=True)

    _write(task_id, report)
    return report


def _shell(cmd: str) -> list[str]:
    return ["/bin/sh", "-c", cmd]


def _fail(
    report: GateReport, step: str, feedback: str, *, evidence_missing: bool = False
) -> GateReport:
    report.passed = False
    report.failing_step = step
    report.steps[step] = "red"
    report.feedback = feedback
    _write(report.task_id, report)
    fb = store.task_data_dir(report.task_id) / "feedback.md"
    fb.write_text(f"# Gate failed at: {step}\n\n{feedback}\n", encoding="utf-8")
    return report


def _write(task_id: str, report: GateReport) -> None:
    d = store.task_data_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "gate.json").write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")


__all__ = ["GateReport", "run_gate", "RepoConfig"]

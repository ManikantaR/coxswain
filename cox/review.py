"""Review pass (step 5) + verdict routing (T-10, DESIGN §2.4).

ONE agent pass, fresh context, sees only the diff + brief + correctness-only
criteria. Findings carry an action: auto-fix (objective) | ask-user (judgment)
| no-op. Routing: all auto-fix -> one resumed fix round; any ask-user ->
needs-human(review-findings); approve -> ship. Parse/crash ->
needs-human(worker-error). There is NO re-run path in this module (DESIGN P2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import models, proc, store
from .lanes.claude import parse_stream_json
from .repoconfig import load_repo_config

_CRITERIA = (
    "Review ONLY for correctness bugs and contract violations against the brief. "
    "Do NOT suggest style, naming, or over-engineering (reviewers always find "
    "something; scope to correctness). For each issue set action to one of: "
    "'auto-fix' (objective, mechanically fixable), 'ask-user' (needs a human "
    "judgment call), or 'no-op' (informational). Reply with ONLY this JSON: "
    '{"findings":[{"severity":"","action":"","summary":"","file":"","line":0}],'
    '"verdict":"approve|fix|reject"}'
)


@dataclass(frozen=True)
class ReviewOutcome:
    route: str  # "approve" | "auto-fix" | "ask-user" | "worker-error"
    verdict: str
    findings: list[dict]
    raw: str = ""


def build_prompt(diff: str, brief: str) -> str:
    return f"# Brief\n{brief}\n\n# Diff\n```diff\n{diff}\n```\n\n# Instructions\n{_CRITERIA}\n"


def _diff(worktree: Path, target: str) -> str:
    r = proc.run(["git", "diff", f"origin/{target}...HEAD"], cwd=worktree, ok_rc=(0, 1, 128))
    return r.out


def parse_review(raw: str) -> ReviewOutcome | None:
    """Parse a review model's JSON reply into a routed outcome, or None if unparseable."""
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    findings = data.get("findings") or []
    verdict = str(data.get("verdict", "")).lower()
    if verdict == "reject" or any(f.get("action") == "ask-user" for f in findings):
        route = "ask-user"
    elif any(f.get("action") == "auto-fix" for f in findings):
        route = "auto-fix"
    else:
        route = "approve"
    return ReviewOutcome(route=route, verdict=verdict or route, findings=findings, raw=raw)


def review(task_id: str) -> ReviewOutcome:
    """Run the single review pass. Never retried (DESIGN P2)."""
    meta = store.load_meta(task_id)
    worktree = Path(meta.worktree)
    cfg = load_repo_config(worktree)
    if cfg.review == "none":
        return ReviewOutcome(route="approve", verdict="approve", findings=[])

    brief = (store.task_data_dir(task_id) / "brief.md").read_text(encoding="utf-8")
    prompt = build_prompt(_diff(worktree, cfg.target_branch), brief)
    spec = models.resolve("reviewer", repo_path=worktree)  # pinned (P8)

    log_path = store.task_data_dir(task_id) / "review.log"
    pid_path = store.task_state_dir(task_id) / "review.pid"
    argv = [
        "claude", "-p", "--model", spec.model,
        "--permission-mode", "plan",  # read-only: the reviewer never edits
        "--output-format", "stream-json", "--verbose", prompt,
    ]  # fmt: skip
    pid = proc.spawn_detached(argv, log_path=log_path, pid_path=pid_path, cwd=worktree)
    # The review is one bounded read-only pass whose verdict the orchestrator
    # needs before it can proceed, so block until the reviewer exits, then parse
    # (shakedown BUG-04 — the old code parsed the log before the worker had
    # written it, always yielding worker-error). An incomplete log after the
    # timeout still parses to the typed worker-error route, never a silent pass.
    _wait_for_exit(pid)
    result = parse_stream_json(log_path, phase="review")
    if result.cost:
        store.append_cost(task_id, result.cost)
    raw = _final_text(log_path)
    outcome = parse_review(raw)
    if outcome is None:
        (store.task_data_dir(task_id) / "review.json").write_text(
            json.dumps({"error": "unparseable", "raw_tail": raw[-2000:]}), encoding="utf-8"
        )
        return ReviewOutcome(route="worker-error", verdict="", findings=[], raw=raw)
    (store.task_data_dir(task_id) / "review.json").write_text(
        json.dumps({"verdict": outcome.verdict, "findings": outcome.findings}, indent=2),
        encoding="utf-8",
    )
    return outcome


def _wait_for_exit(pid: int, timeout: float = 900.0) -> None:
    """Block until the reviewer process exits (or timeout). Read-only, bounded."""
    import time

    t0 = time.time()
    while proc.is_alive(pid) and time.time() - t0 < timeout:
        time.sleep(1.0)


def _final_text(log_path: Path) -> str:
    if not log_path.exists():
        return ""
    result_text = ""
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            result_text = str(obj.get("result", ""))
    return result_text

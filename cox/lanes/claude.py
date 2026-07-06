"""Claude Code lane (T-07). Flags verified live in docs/CLI-FACTS.md.

Spawn: `claude -p "<brief>" --model <m> --permission-mode acceptEdits
--allowedTools ... --output-format stream-json`, detached, log -> file.
Resume (fix round): `claude -p --resume <session_id> "<feedback>" ...` run from
the SAME worktree cwd (session lookup is cwd-scoped). parse_result reads the
final result object from the stream-json log for session_id + usage + cost.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import proc
from ..model import CostEntry, ModelSpec
from .base import RunResult, SpawnHandle

# Tools a worker needs to do real work and commit, but NOT push (trust boundary,
# DESIGN P6 — push creds are withheld from the worker env by dispatch).
_ALLOWED_TOOLS = "Edit,Write,Read,Bash(git*),Bash(pytest*),Bash(npm*),Bash(ruff*)"


class ClaudeLane:
    name = "claude"

    def _base_argv(self, model: ModelSpec) -> list[str]:
        argv = [
            "claude",
            "-p",
            "--model",
            model.model,
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            _ALLOWED_TOOLS,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        return argv

    def spawn(
        self, brief_path: Path, worktree: Path, model: ModelSpec, log_path: Path, pid_path: Path
    ) -> SpawnHandle:
        brief = brief_path.read_text(encoding="utf-8")
        # The worker's status.log + evidence dir live in the task data dir, which
        # is OUTSIDE the worktree. Claude Code sandboxes writes to the cwd unless
        # extra dirs are allowed, so grant the data dir (brief.md's parent) — else
        # the liveness/evidence protocol is silently unwritable (shakedown finding
        # 2026-07-05). --add-dir verified live in docs/CLI-FACTS.md.
        data_dir = brief_path.parent
        argv = self._base_argv(model) + ["--add-dir", str(data_dir), brief]
        pid = proc.spawn_detached(
            argv, log_path=log_path, pid_path=pid_path, cwd=worktree, env=_worker_env()
        )
        return SpawnHandle(pid=pid, log_path=log_path)

    def resume(
        self, session_id: str, feedback: str, worktree: Path, log_path: Path, pid_path: Path
    ) -> SpawnHandle:
        # Model is carried by the resumed session; --resume must run from the
        # original worktree cwd (CLI-FACTS.md). data dir (worker.log's parent)
        # granted so the fix round can write status/evidence too (see spawn()).
        data_dir = log_path.parent
        argv = [
            "claude",
            "-p",
            "--resume",
            session_id,
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            _ALLOWED_TOOLS,
            "--output-format",
            "stream-json",
            "--verbose",
            "--add-dir",
            str(data_dir),
            feedback,
        ]
        pid = proc.spawn_detached(
            argv, log_path=log_path, pid_path=pid_path, cwd=worktree, env=_worker_env()
        )
        return SpawnHandle(pid=pid, log_path=log_path)

    def parse_result(self, log_path: Path, phase: str) -> RunResult:
        return parse_stream_json(log_path, phase)


def _worker_env() -> dict[str, str]:
    """Env with push credentials stripped (DESIGN P6). The control plane pushes."""
    import os

    env = dict(os.environ)
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        env.pop(key, None)
    return env


def parse_stream_json(log_path: Path, phase: str) -> RunResult:
    """Extract the final result object from a stream-json log.

    The last line of type "result" carries session_id, total_cost_usd and
    usage. Malformed/absent -> parse-error (-> needs-human worker-error, never
    retried, DESIGN P2).
    """
    if not log_path.exists():
        return RunResult(outcome="parse-error", session_id=None, cost=None, raw_tail="(no log)")

    result_obj: dict | None = None
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            result_obj = obj

    if result_obj is None:
        tail = "\n".join(log_path.read_text(encoding="utf-8").splitlines()[-20:])
        return RunResult(outcome="parse-error", session_id=None, cost=None, raw_tail=tail)

    usage = result_obj.get("usage", {}) or {}
    tokens_in = int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0))
    tokens_out = int(usage.get("output_tokens", 0))
    cost = CostEntry(
        phase=phase,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=result_obj.get("total_cost_usd"),
    )
    subtype = result_obj.get("subtype", "")
    is_error = bool(result_obj.get("is_error"))
    outcome = "success" if (subtype == "success" and not is_error) else "failed"
    return RunResult(
        outcome=outcome, session_id=result_obj.get("session_id"), cost=cost, raw_tail=""
    )

"""Lane protocol + the one retry rule that can never be configured away.

A lane adapts one agent harness (claude, codex, copilot, or the test stub) to
a tiny interface: spawn a worker, resume it for a fix round, and parse the
result log into a session id + usage + outcome (DESIGN §2.7).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..model import CostEntry, ModelSpec

# DESIGN P2: no agent invocation is ever auto-retried. Infra failure -> typed
# needs-human, never a re-run. This is a module constant with NO config
# override on purpose — relay's reviewer retry loop was its #1 token burner.
AGENT_RETRY_CAP = 1


@dataclass(frozen=True)
class SpawnHandle:
    pid: int
    log_path: Path


@dataclass(frozen=True)
class RunResult:
    """Parsed outcome of one worker run.

    outcome is one of: success | failed | blocked | parse-error. session_id is
    None when the lane could not report one; cost may be None for lanes that
    don't emit usage (logged loudly, DESIGN §4.9).
    """

    outcome: str
    session_id: str | None
    cost: CostEntry | None
    raw_tail: str = ""


class Lane(Protocol):
    name: str

    def spawn(
        self, brief_path: Path, worktree: Path, model: ModelSpec, log_path: Path, pid_path: Path
    ) -> SpawnHandle: ...

    def resume(
        self, session_id: str, feedback: str, worktree: Path, log_path: Path, pid_path: Path
    ) -> SpawnHandle: ...

    def parse_result(self, log_path: Path, phase: str) -> RunResult: ...

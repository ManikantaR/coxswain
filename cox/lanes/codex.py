"""Codex lane (T-15, M1). Flags verified in docs/CLI-FACTS.md.

Placeholder until M1: the claude lane must ship first (ROADMAP P10). When
built: `codex exec --json -m <model> -s workspace-write "<brief>"`; session id
is the thread_id from the first `thread.started` event; resume via
`codex exec resume <thread_id> "<feedback>"`. Codex reports tokens but no cost,
so CostEntry.cost_usd is None (logged, not hidden).
"""

from __future__ import annotations

from pathlib import Path

from ..model import ModelSpec
from .base import RunResult, SpawnHandle


class CodexLane:
    name = "codex"

    def spawn(
        self, brief_path: Path, worktree: Path, model: ModelSpec, log_path: Path, pid_path: Path
    ) -> SpawnHandle:
        raise NotImplementedError("codex lane lands in M1 / T-15")

    def resume(
        self, session_id: str, feedback: str, worktree: Path, log_path: Path, pid_path: Path
    ) -> SpawnHandle:
        raise NotImplementedError("codex lane lands in M1 / T-15")

    def parse_result(self, log_path: Path, phase: str) -> RunResult:
        raise NotImplementedError("codex lane lands in M1 / T-15")

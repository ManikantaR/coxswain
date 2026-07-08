"""Typed core: task states, needs-human reasons, dispatch paths, records.

Everything here is a frozen dataclass or a str enum so state lives on disk as
plain JSON and mypy --strict can guard the transitions (DESIGN §2.3, §2.9).
No I/O in this module — see store.py.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class TaskState(StrEnum):
    QUEUED = "queued"
    WORKING = "working"
    GATING = "gating"
    FIXING = "fixing"
    PR_OPEN = "pr_open"
    LANDED = "landed"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"


class NeedsHumanReason(StrEnum):
    """Closed enum — a needs-human task ALWAYS carries one of these (DESIGN P5).

    This is the anti-"dumping ground" design: relay's single needs_decision
    bucket collapsed ~8 unrelated causes. Each reason has its own recovery
    verbs in ORCHESTRATOR.md.
    """

    GATE_RED = "gate-red"
    REVIEW_FINDINGS = "review-findings"
    WORKER_ERROR = "worker-error"
    WORKER_STALE = "worker-stale"
    PUSH_REJECTED = "push-rejected"
    PR_ERROR = "pr-error"
    CI_RED = "ci-red"
    RATE_LIMITED = "rate-limited"
    EVIDENCE_MISSING = "evidence-missing"


class DispatchPath(StrEnum):
    INLINE = "inline"
    QUICK = "quick"
    FULL = "full"


@dataclass(frozen=True)
class ModelSpec:
    """A pinned model for one agent invocation. Never unpinned (DESIGN P8)."""

    provider: str
    model: str
    effort: str = "medium"


@dataclass(frozen=True)
class CostEntry:
    """One agent invocation's usage, appended to state/<id>/cost.jsonl (P9).

    cost_usd is None for lanes that report tokens but not dollars (codex) or
    nothing at all (copilot) — those log usage loudly rather than silently.
    """

    phase: str
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    ts: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskMeta:
    """Durable identity + state of one task (state/<id>/meta.json)."""

    id: str
    repo: str
    worktree: str
    branch: str
    lane: str
    model: str
    path: DispatchPath
    state: TaskState = TaskState.QUEUED
    reason: NeedsHumanReason | None = None
    session_id: str | None = None
    pr_url: str | None = None
    fix_rounds: int = 0
    dispatched_at: float = field(default_factory=time.time)
    # Review slot (DESIGN-VNEXT D14): pinned independently of implement. None =
    # fall back to the resolved reviewer default (currently opus). review can
    # cross providers freely (it is stateless — reads only the diff).
    review_lane: str | None = None
    review_model: str | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["path"] = self.path.value
        d["state"] = self.state.value
        d["reason"] = self.reason.value if self.reason else None
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> TaskMeta:
        return cls(
            id=d["id"],
            repo=d["repo"],
            worktree=d["worktree"],
            branch=d["branch"],
            lane=d["lane"],
            model=d["model"],
            path=DispatchPath(d["path"]),
            state=TaskState(d["state"]),
            reason=NeedsHumanReason(d["reason"]) if d.get("reason") else None,
            session_id=d.get("session_id"),
            pr_url=d.get("pr_url"),
            fix_rounds=d.get("fix_rounds", 0),
            dispatched_at=d.get("dispatched_at", 0.0),
            review_lane=d.get("review_lane"),
            review_model=d.get("review_model"),
        )

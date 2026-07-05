"""Lane registry. M0: claude + stub. M1 adds codex; V1 adds copilot."""

from __future__ import annotations

from .base import Lane


def get_lane(name: str) -> Lane:
    if name == "claude":
        from .claude import ClaudeLane

        return ClaudeLane()
    if name == "stub":
        from .stub import StubLane

        return StubLane()
    if name == "codex":  # T-15
        from .codex import CodexLane

        return CodexLane()
    raise ValueError(f"unknown lane {name!r} (available: claude, codex, stub)")

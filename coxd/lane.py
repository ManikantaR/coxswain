"""Claude lane — worker + reviewer over the Agent SDK (DESIGN-V35).

Replaces cox/proc.py + lanes/*.py + _worker_env + review._wait_for_exit. The
worker runs in an isolated cwd with a PreToolUse hook that hard-denies `git push`;
fix rounds RESUME the same session (native, cheap). The reviewer is stateless,
a different model, read-only. All usage/cost is structured — no log parsing.
Every interesting message is handed to an `on_event(kind, data)` callback the
supervisor persists (the event log). codex lane is a later swap behind this shape.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

Emit = Callable[[str, dict], None]


async def _no_push_hook(input_data: dict, tool_use_id: str | None, context: object):
    if input_data.get("tool_name") == "Bash":
        cmd = str((input_data.get("tool_input") or {}).get("command", ""))
        if "git push" in cmd:
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason":
                    "coxswain no-push boundary: workers never push; the control plane does.",
            }}
    return {}


@dataclass
class WorkerResult:
    session_id: str | None
    cost: float | None
    is_error: bool


async def run_worker(worktree: Path, prompt: str, model: str, emit: Emit,
                     resume: str | None = None) -> WorkerResult:
    """Implement (or fix, if `resume` is set) in the worktree. No push allowed."""
    options = ClaudeAgentOptions(
        model=model, cwd=str(worktree),
        allowed_tools=["Bash", "Write", "Read", "Edit", "Glob", "Grep"],
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[_no_push_hook])]},
        permission_mode="bypassPermissions",
        resume=resume, max_turns=60,
    )
    result: ResultMessage | None = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, RateLimitEvent):
                emit("rate_limit", {"info": str(msg.rate_limit_info)})
            elif isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, ToolUseBlock):
                        emit("tool", {"name": b.name, "input": str(b.input)[:200]})
                    elif isinstance(b, TextBlock) and b.text.strip():
                        emit("say", {"text": b.text.strip()[:200]})
            elif isinstance(msg, ResultMessage):
                result = msg
    if result is None:
        return WorkerResult(None, None, True)
    return WorkerResult(result.session_id, result.total_cost_usd,
                        bool(result.is_error or getattr(result, "api_error_status", None)))


_CRITERIA = (
    "You are a correctness-only reviewer. Review ONLY for bugs and contract "
    "violations (no style). Reply with ONLY JSON: "
    '{"findings":[{"severity":"high|med|low","summary":"","file":"","line":0}],'
    '"verdict":"approve|fix|reject"}'
)


@dataclass
class ReviewOutcome:
    outcome: str  # "reviewed" | "review-error" (retryable — NEVER cached as a verdict)
    verdict: str | None
    findings: list[dict]
    cost: float | None


async def review(diff: str, model: str, emit: Emit) -> ReviewOutcome:
    options = ClaudeAgentOptions(model=model, permission_mode="plan",
                                 allowed_tools=[], max_turns=1)
    text, result = "", None
    async for msg in query(prompt=f"# Diff\n```\n{diff}\n```\n\n{_CRITERIA}", options=options):
        if isinstance(msg, RateLimitEvent):
            emit("rate_limit", {"info": str(msg.rate_limit_info)})
        elif isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    text += b.text
        elif isinstance(msg, ResultMessage):
            result = msg
    if result is None or result.is_error or getattr(result, "api_error_status", None):
        return ReviewOutcome("review-error", None, [], result.total_cost_usd if result else None)
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        return ReviewOutcome("review-error", None, [], result.total_cost_usd)
    try:
        d = json.loads(text[s : e + 1])
    except json.JSONDecodeError:
        return ReviewOutcome("review-error", None, [], result.total_cost_usd)
    return ReviewOutcome("reviewed", str(d.get("verdict", "")).lower() or None,
                         d.get("findings") or [], result.total_cost_usd)

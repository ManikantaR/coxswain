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
import os
import re
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


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason}}


def _under(raw: str, wt: Path) -> bool:
    """Does path `raw` (abs, ~, or relative-to-worktree) resolve inside the worktree?"""
    p = Path(os.path.expanduser(raw.strip().strip('"').strip("'")))
    if not p.is_absolute():
        p = wt / p
    try:
        p.resolve().relative_to(wt.resolve())
        return True
    except (ValueError, OSError):
        return False


# `cd`/`pushd <target>` and `> / >>` redirect targets — the two accidental escape
# vectors the #98 run exposed (a worker `cd`-ed to the main checkout and wrote to it).
_CD = re.compile(r"\b(?:cd|pushd)\s+([^\s;&|<>]+)")
_REDIR = re.compile(r">>?\s*([^\s;&|<>]+)")
_BARE_CD = re.compile(r"\bcd\b\s*(?:;|&|\||$)")


def _make_boundary_hook(worktree: Path):
    """Hard-deny anything that leaves the worktree (Bash cd/redirect, Write/Edit path).

    cwd is only a *default*, not a sandbox — bypassPermissions workers can `cd` out
    or write to an absolute path. This is the boundary cwd doesn't give us; same
    mechanism as no-push, which it also enforces.
    """
    async def hook(input_data: dict, tool_use_id: str | None, context: object):
        tool = input_data.get("tool_name")
        ti = input_data.get("tool_input") or {}
        if tool == "Bash":
            cmd = str(ti.get("command", ""))
            if "git push" in cmd:
                return _deny("coxswain no-push boundary: workers never push; "
                             "the control plane does.")
            if _BARE_CD.search(cmd):
                return _deny("coxswain worktree boundary: bare `cd` leaves the worktree. "
                             f"Stay inside {worktree}.")
            for target in _CD.findall(cmd) + _REDIR.findall(cmd):
                if not _under(target, worktree):
                    return _deny(
                        f"coxswain worktree boundary: `{target}` is outside the worktree. "
                        f"Do all work inside {worktree}; never touch other checkouts.")
        elif tool in ("Write", "Edit"):
            fp = str(ti.get("file_path", ""))
            if fp and not _under(fp, worktree):
                return _deny(
                    f"coxswain worktree boundary: cannot write `{fp}` outside the worktree. "
                    f"Do all work inside {worktree}.")
        return {}
    return hook


@dataclass
class WorkerResult:
    session_id: str | None
    cost: float | None
    is_error: bool


# SEED rubric — ONE source of truth both lanes read (implementor as the bar to clear,
# reviewer as what blocks vs. what is advisory). Deliberately correctness/security-only
# so Opus-review blocks on real defects, not taste (an over-strict reviewer stalls the
# loop against the <=1-unstick bar). This is a SEED: sharpen it from the observed failure
# log across the #102-#108 batch, do not gold-plate it up front.
_RUBRIC = (
    "Bar (blocking pillars — a defect here is a real bug, not a preference):\n"
    "1. Correctness: logic, edge/empty/null cases, off-by-one, wrong async/await, "
    "unhandled error paths, state that can desync.\n"
    "2. Security (OWASP-critical subset only): injection (SQL/command/template), "
    "broken authn/authz or missing ownership checks, secrets/keys in code, unsafe "
    "deserialization, SSRF, missing input validation on a trust boundary.\n"
    "3. Data/contract integrity: API request/response or DB schema/migration changes "
    "that break existing callers or data; irreversible or non-idempotent migrations.\n"
    "Advisory ONLY (record as low findings, do NOT block): style, naming, formatting, "
    "micro-optimizations, test-coverage nits, refactor suggestions.\n"
)


# Standing operating instruction prepended to EVERY worker turn (implement + fix).
# Closes three #98-run bugs: (1) the worker hunted the main checkout because the
# linked worktree's `.git` FILE names it — so we state the cwd explicitly; (2) it
# never committed, leaving the gate/ship nothing — so we require a commit; (3) it
# burned its whole turn budget re-verifying — so we tell it to stop when done.
_PREAMBLE = (
    "Your working directory is {wt} — the project lives HERE, in this git worktree. "
    "Do ALL work inside it; never `cd` to or edit any other checkout (the `.git` file "
    "names a different path — ignore it). When the acceptance criteria are met: `git add` "
    "and `git commit` your work (do NOT push), then STOP. Do not re-verify repeatedly.\n\n"
    "Your code must clear this review bar before it can land:\n" + _RUBRIC + "\n"
)


async def run_worker(worktree: Path, prompt: str, model: str, emit: Emit,
                     resume: str | None = None) -> WorkerResult:
    """Implement (or fix, if `resume` is set) in the worktree. No push allowed."""
    boundary = _make_boundary_hook(worktree)
    options = ClaudeAgentOptions(
        model=model, cwd=str(worktree),
        allowed_tools=["Bash", "Write", "Read", "Edit", "Glob", "Grep"],
        hooks={"PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[boundary]),
            HookMatcher(matcher="Write", hooks=[boundary]),
            HookMatcher(matcher="Edit", hooks=[boundary]),
        ]},
        permission_mode="bypassPermissions",
        resume=resume, max_turns=100,
    )
    result: ResultMessage | None = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(_PREAMBLE.format(wt=worktree) + prompt)
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
    "You are the merge-gate reviewer. Judge the diff against this bar:\n\n"
    + _RUBRIC
    + "\nBlocking policy (calibrate the verdict to this — do NOT block on advisory items):\n"
    "- verdict `fix`  = one or more high/med defects in a BLOCKING pillar (1-3 above).\n"
    "- verdict `reject` = the change is fundamentally wrong or unsafe to land at all.\n"
    "- verdict `approve` = no blocking-pillar defects; advisory findings may still be listed.\n\n"
    "Reply with ONLY JSON: "
    '{"findings":[{"severity":"high|med|low","pillar":"correctness|security|contract|advisory",'
    '"summary":"","file":"","line":0}],"verdict":"approve|fix|reject"}'
)


@dataclass
class ReviewOutcome:
    outcome: str  # "reviewed" | "review-error" (retryable — NEVER cached as a verdict)
    verdict: str | None
    findings: list[dict]
    cost: float | None


async def review(diff: str, model: str, emit: Emit) -> ReviewOutcome:
    # No tools, no plan mode: the reviewer just emits JSON. plan mode would need an
    # ExitPlanMode call to finish (which allowed_tools=[] forbids) -> it never completes
    # and the SDK RAISES "max turns"; a couple of turns of headroom + a hard guard so any
    # infra failure becomes the typed review-error (never a crash, never a fake verdict).
    options = ClaudeAgentOptions(model=model, permission_mode="bypassPermissions",
                                 allowed_tools=[], max_turns=4)
    text, result = "", None
    try:
        async for msg in query(prompt=f"# Diff\n```\n{diff}\n```\n\n{_CRITERIA}", options=options):
            if isinstance(msg, RateLimitEvent):
                emit("rate_limit", {"info": str(msg.rate_limit_info)})
            elif isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        text += b.text
            elif isinstance(msg, ResultMessage):
                result = msg
    except Exception as e:  # SDK raises on max-turns / transport faults — type it, don't crash
        emit("review-error", {"err": str(e)[:200]})
        return ReviewOutcome("review-error", None, [], None)
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

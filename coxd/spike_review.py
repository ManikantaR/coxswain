"""V3.5 spike 2c — review lane with the Run-B safeguards (DESIGN-V35 §4).

Run B lost $0.61: the reviewer launched into a 97%-exhausted window, a 900s wait
timed out (indistinguishable from exit), a half-written log parsed to
worker-error, and P2 CACHED that infra error as a permanent verdict. The SDK
removes the whole class:
 - a clean `ResultMessage` (no `_wait_for_exit` race, no log tailing);
 - `is_error` / `api_error_status` -> a TYPED, retryable review-error, NEVER a
   verdict (so it is never cached as "reject");
 - `RateLimitEvent` surfaced from the stream (the pre-flight signal Run B ignored).
Reviewer runs read-only (no tools); a different model than the worker = the
cross-model second opinion. codex-sdk (alpha) is a later swap behind Lane.

Run: coxd/.venv/bin/python coxd/spike_review.py
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    query,
)

DIFF = """--- a/stats.py
+++ b/stats.py
@@
+def average(nums):
+    return sum(nums) / len(nums)
"""

CRITERIA = (
    "You are a correctness-only reviewer. Review the diff for BUGS and contract "
    "violations only (no style). Reply with ONLY JSON: "
    '{"findings":[{"severity":"high|med|low","summary":"","file":"","line":0}],'
    '"verdict":"approve|fix|reject"}'
)


@dataclass
class ReviewOutcome:
    outcome: str  # "reviewed" | "review-error" (retryable — never cached as a verdict)
    verdict: str | None
    findings: list[dict]
    cost: float | None
    rate_limited: bool


def _parse(text: str) -> tuple[str | None, list[dict]] | None:
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        return None
    try:
        d = json.loads(text[s : e + 1])
    except json.JSONDecodeError:
        return None
    return str(d.get("verdict", "")).lower() or None, d.get("findings") or []


async def review(diff: str) -> ReviewOutcome:
    options = ClaudeAgentOptions(
        model="claude-sonnet-4-6",  # different model than the (haiku) worker
        permission_mode="plan",      # read-only: the reviewer never edits
        allowed_tools=[],
        max_turns=1,
    )
    text, result, rate_limited = "", None, False
    async for msg in query(prompt=f"# Diff\n```\n{diff}\n```\n\n{CRITERIA}", options=options):
        if isinstance(msg, RateLimitEvent):
            rate_limited = True
            print(f"  ! RateLimitEvent: {msg}")
        elif isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    text += b.text
        elif isinstance(msg, ResultMessage):
            result = msg

    # infra failure -> typed retryable error, NEVER a verdict (Run-B hole #5)
    if result is None or result.is_error or getattr(result, "api_error_status", None):
        return ReviewOutcome("review-error", None, [],
                             result.total_cost_usd if result else None, rate_limited)
    parsed = _parse(text)
    if parsed is None:
        return ReviewOutcome("review-error", None, [], result.total_cost_usd, rate_limited)
    verdict, findings = parsed
    return ReviewOutcome("reviewed", verdict, findings, result.total_cost_usd, rate_limited)


async def main() -> int:
    print("reviewing a diff with an obvious bug (÷ by len on a possibly-empty list)...")
    r = await review(DIFF)
    print("\n--- outcome ---")
    print(f"  outcome      : {r.outcome}")
    print(f"  verdict      : {r.verdict}")
    print(f"  findings     : {json.dumps(r.findings)[:300]}")
    print(f"  cost         : ${r.cost}")
    print(f"  rate_limited : {r.rate_limited}")
    ok = r.outcome == "reviewed" and r.verdict in ("fix", "reject") and len(r.findings) >= 1
    print("\nVERDICT:", "✓ clean review; infra-error path is typed+retryable (never a cached verdict)"
          if ok else "✗ investigate (note: an infra/rate-limit error here is EXPECTED to be "
                     "review-error, not a failure of the spike)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

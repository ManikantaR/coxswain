"""V3.5 spike 1 — billing proof (DESIGN-V35 §4, week 1, step 1).

Make-or-break: does the Claude Agent SDK run on the Pro SUBSCRIPTION (via
`claude login` OAuth) rather than a per-token API key? The proof: assert
ANTHROPIC_API_KEY is absent, run one real session, and confirm it succeeds and
reports usage. Success with no API key == subscription auth (an API-key-only path
would fail auth). Run: coxd/.venv/bin/python coxd/spike_billing.py
"""

from __future__ import annotations

import asyncio
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)


async def main() -> int:
    # 1. Subscription-billing invariant: the API key must be ABSENT.
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    print(f"ANTHROPIC_API_KEY: {'SET — WOULD BILL PER-TOKEN ✗' if api_key else 'unset ✓'}")
    if api_key:
        print("Refusing: unset ANTHROPIC_API_KEY so the SDK uses the subscription login.")
        return 1

    options = ClaudeAgentOptions(
        model="claude-haiku-4-5",  # cheapest model — this is a billing probe, not work
        max_turns=1,
        allowed_tools=[],  # no tools: a pure round-trip
    )

    said = None
    result: ResultMessage | None = None
    async for msg in query(prompt="Reply with exactly the word PONG, nothing else.", options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    said = block.text.strip()
        elif isinstance(msg, ResultMessage):
            result = msg

    print(f"assistant said: {said!r}")
    if result is None:
        print("NO ResultMessage — SDK stream did not complete ✗")
        return 1
    print("--- ResultMessage ---")
    print(f"  is_error      : {result.is_error}")
    print(f"  model         : {options.model}")
    print(f"  session_id    : {result.session_id}")
    print(f"  num_turns     : {result.num_turns}")
    print(f"  duration_ms   : {result.duration_ms}")
    print(f"  total_cost_usd: {result.total_cost_usd}")
    print(f"  usage         : {result.usage}")
    ok = (not result.is_error) and said is not None and "PONG" in (said or "").upper()
    print()
    print("VERDICT:", "✓ ran on the subscription (no API key), structured usage returned"
          if ok else "✗ failed — investigate")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

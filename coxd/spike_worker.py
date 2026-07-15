"""V3.5 spike 2a — the worker primitive (DESIGN-V35 §4, week 1, step 2).

Proves the core nervous-system replacement: a Claude Agent SDK session that does
real file+git work inside an ISOLATED directory (cwd), where a `can_use_tool`
callback HARD-DENIES `git push` — the no-push trust boundary as a first-class
policy, not the old env-var credential-stripping dance. Replaces cox/proc.py +
lanes/*.py argv-building + _worker_env, with structured cost/session from the SDK.

Self-contained: builds a throwaway git repo in a temp dir, no real repos touched.
Run: coxd/.venv/bin/python coxd/spike_worker.py
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

DENIED: list[str] = []  # commands the no-push boundary blocked


async def no_push_hook(input_data: dict, tool_use_id: str | None, context: object):
    """PreToolUse gate: hard-deny `git push` on every Bash call (fires regardless
    of permission mode). Replaces the old env-var credential-stripping."""
    if input_data.get("tool_name") == "Bash":
        cmd = str((input_data.get("tool_input") or {}).get("command", ""))
        if "git push" in cmd:
            DENIED.append(cmd)
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason":
                    "coxswain no-push boundary: workers never push; the control plane does.",
            }}
    return {}


def _init_repo() -> Path:
    d = Path(tempfile.mkdtemp(prefix="coxd-worker-"))
    run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q")
    run("git", "config", "user.email", "coxd@spike")
    run("git", "config", "user.name", "coxd")
    (d / "README.md").write_text("scratch\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "init")
    # a fake remote so `git push` has somewhere to (try to) go — it must never reach it
    bare = Path(tempfile.mkdtemp(prefix="coxd-remote-")) / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    run("git", "remote", "add", "origin", str(bare))
    return d


async def main() -> int:
    worktree = _init_repo()
    print(f"worktree: {worktree}")

    task = (
        "In the current directory: create a file hello.txt containing exactly HELLO, "
        "commit it with git (message 'add hello'), then push it to origin main."
    )
    options = ClaudeAgentOptions(
        model="claude-haiku-4-5",
        cwd=str(worktree),
        allowed_tools=["Bash", "Write", "Read", "Edit"],
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[no_push_hook])]},
        permission_mode="bypassPermissions",  # headless; the hook is the real gate
        max_turns=12,
    )

    tools_used, result = [], None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(task)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, ToolUseBlock):
                        tools_used.append(b.name)
                        print(f"  → {b.name}: {str(b.input)[:80]}")
                    elif isinstance(b, TextBlock) and b.text.strip():
                        print(f"  · {b.text.strip()[:100]}")
            elif isinstance(msg, ResultMessage):
                result = msg

    committed = subprocess.run(
        ["git", "log", "--oneline"], cwd=worktree, capture_output=True, text=True
    ).stdout
    file_ok = (worktree / "hello.txt").exists() and \
        (worktree / "hello.txt").read_text().strip() == "HELLO"

    print("\n--- result ---")
    print(f"  file hello.txt == HELLO : {file_ok}")
    print(f"  committed               : {'add hello' in committed}")
    print(f"  push DENIED by boundary : {bool(DENIED)}  {DENIED}")
    print(f"  cost/session            : ${result.total_cost_usd if result else '?'} "
          f"/ {result.session_id if result else '?'}")
    ok = file_ok and "add hello" in committed and bool(DENIED)
    print("\nVERDICT:", "✓ worker did the work in isolation AND could not push"
          if ok else "✗ investigate")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

# V3.5 build annex — SDK facts from the 2026-07-14 research pass

Implementation reference for the DESIGN-V35 build slice. Conclusions live in
DESIGN-V35.md; this is the how/where detail so a fresh session doesn't re-research.

## Claude Agent SDK (the worker/plan lane)

- Python `claude-agent-sdk` / TS near-parity; wraps the Claude Code runtime; Windows
  supported (TS bundles a native binary). Docs: code.claude.com/docs/en/agent-sdk/overview
- **Billing: rides Pro/Max subscription** when authenticated via `claude login`
  (OAuth creds on disk); the SDK picks up local Claude Code credentials.
  **Setting `ANTHROPIC_API_KEY` flips to per-token API billing — coxd must assert
  it is ABSENT from its env.** Source: support.claude.com article
  15036540 "Use the Claude Agent SDK with your Claude plan".
- Watch item: Anthropic announced then PAUSED a separate headless credit pool
  ($20/mo Pro, $100/$200 Max) for Agent SDK / `claude -p` / Actions. Today it draws
  the same subscription quota. Never silently pay-as-you-go either way.
- ToS: third parties may not offer claude.ai login to THEIR customers; personal use
  of your own login (same posture as `claude -p` scripts) is explicitly fine.
- What we use (replaces our plumbing):
  - `query()` / `ClaudeSDKClient` — child-process lifecycle managed; typed async
    message stream (SystemMessage / assistant / tool / **ResultMessage with
    usage+cost per turn**). No log files, no tailing, no pid files.
  - `resume=session_id` first-class (+ fork). Sessions persist as SDK-managed JSONL;
    resume survives supervisor restarts — this is the crash-recovery story.
  - **Hooks (in-process callbacks): PreToolUse / PostToolUse / Stop /
    SessionStart/End / UserPromptSubmit.** `Stop` hook = run the deterministic gate
    and block a blind stop; `PreToolUse` = hard-deny `git push` (workers physically
    can't push). This replaces watch.py + wakequeue + log-grep gate detection.
  - `permission_mode` + `canUseTool` callback — plan approval / dangerous tools as
    awaitable callbacks the board answers; `AskUserQuestion` for typed needs-human.
  - Subagents via `AgentDefinition` with per-agent `model` (pinning preserved).
  - `setting_sources` controls filesystem config loading — inject system prompt /
    skills / MCP programmatically from the central registry (no repo files).
  - Concurrency: it's a library — one asyncio supervisor runs N sessions as tasks.

## Codex SDK (the reviewer lane)

- TS `@openai/codex-sdk`; Python `openai-codex` (JSON-RPC to the local codex
  app-server). Threads, resume by thread id, sandbox presets, structured output.
  Docs: developers.openai.com/codex/sdk
- **Billing: `codex login` rides the ChatGPT plan** (incl. scripted use); API-key
  sign-in flips to per-token. Source: help.openai.com article 11369540.
- Replaces our `codex exec --json` wrap + JSONL scraping.

## Copilot (future work lane — stub only, per D22)

- Copilot SDK GA 2026-06-02 (Node/Py/Go/.NET/Rust/Java): sessions, streaming,
  hooks, OTel, GitHub OAuth; any Copilot plan. Session/hook shape deliberately
  similar to Agent SDK → thin AgentLane adapter is plausible LATER.
- Copilot coding agent (cloud, issue→PR, agents panel "mission control") is
  GitHub-native — the SDK is repo-host-agnostic (works against ADO git), the cloud
  agent is not. Do not build this before the work deployment is real.

## Patterns adopted from the field

- firstmate (kunchenguid): brain-is-an-agent + zero-token bash watcher + central
  per-project config in the orchestrator home; **code only does token-free
  deterministic work**. no-mistakes: the gate as a local git push proxy
  (intercept push → worktree → test/review → forward when green).
- Mission-control convergence (Copilot panel, Devin, Agent Teams, Conductor):
  board of tasks not terminals; steering = inject message into a live session;
  approvals = blocking prompts in ONE inbox; durable state = runtime-owned session
  log, UI stateless over it. Minimum viable: task list + status + needs-you
  notifications + diff + merge button.
- Per-repo config elimination: Devin Knowledge (central, per-repo, auto-suggested
  learnings); Claude/Codex just read package.json/Makefile/CI. → our registry:
  `~/.coxswain/repos/<name>` written by a one-time scout session, human-editable.

## Run-B defects the new code must not reproduce

1. cached tokens are a SUBSET of input in codex usage (and cache_read separate in
   claude) — never sum them into tokens_in; report fresh vs cached separately.
2. A timeout is not an exit: distinguish them, kill orphans, never parse a
   half-written stream (moot with SDK streams, but applies to gate subprocesses).
3. Never cache an infra error as a review verdict (P2 = completed reviews only).
4. Pre-flight the subscription window before spawning a paid session (the failed
   review launched at ~97% five-hour utilization).
5. Gate "skip" on a full task is RED (a gate that can lie is worse than none).

## In-flight state at pivot time

- MoneyPulse #99: implement DONE (worktree branch, commit 3e4123b, 17 files,
  63/63 tests in evidence), gate passed, agent review discarded by the timeout bug.
  Disposition: ship WITHOUT agent re-review (infra failure, not quality signal);
  captain reviews the PR himself. Worktree preserved until shipped.
- #98's remaining ~9 issues = the v3.5 acceptance backlog (DESIGN-V35 §3).
- Old dashboard/watcher processes are session-bound on the Mac; they die with the
  session and that's acceptable until coxd lands on the NAS.

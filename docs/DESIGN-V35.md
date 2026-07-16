# Coxswain v3.5 — control-plane transplant (THE CONTRACT)

Locked 2026-07-14 after a two-agent research pass: (1) forensic post-mortem of all
three attempts (agent-orchestrator → relay → coxswain) + this week's two real runs;
(2) strategy research on SDK-based orchestration. Grilled to shared understanding.
This document is a CONTRACT, not a roadmap: §3 defines done; until it is met,
**no new surface may be built** (§4), and the assistant is instructed to refuse.

## 1. Why (the post-mortem, one paragraph each)

**The disease:** 14 of 14 real-run failures lived in the hand-rolled transport —
argv construction, pid files, JSONL log tailing, parse-before-done races, polling
watcher, wrong-log dashboard. Zero were agent-quality failures. 4 phases × 2 lanes
× 5 hand-rolled mechanisms with three uncoordinated state-owners (CLI, watcher,
LLM-reading-prose) regenerates the same bug class every run; hardening is a
treadmill. ~35–40% of the code is plumbing an SDK provides; weighted by defects
it is ~100% of the failure surface.

**The accounting lied:** Run B's "16M tokens" was a double-count (codex `cached_input`
is a SUBSET of `input`; the parser added them — real fresh input ~146k, 98% cached).
Opus's "485k read" was 99.9% cache-reads. The real waste: a $0.61 review discarded by
a silent 900s timeout (timeout indistinguishable from exit), the orphan burning 40
more minutes, and P2 caching the INFRA error as a permanent verdict. On flat rate the
scarce resources burned were the captain's time and trust.

**The ratchet:** all three attempts wrote the prior lesson into a locked doc, then
overrode it within days (D6 "NO dashboard" lasted 3; M0's "10 real tasks" is still
owed while 5 of 7 dispatched tasks were the orchestrator building itself). Docs have
never held the line — hence a contract with an enforcer.

## 2. The locked decisions (D19–D26 in DECISIONS.md)

1. **Rebuild the control plane on the Claude Agent SDK** (v3.5 — a transplant, not
   attempt 4). One long-running supervisor (`coxd`, Python asyncio); each task is one
   async function `plan → implement → gate → review → ship` as awaited calls over SDK
   sessions. Deletes: proc.py, watch.py, wakequeue.py, classify.py, render.py,
   context.py, pid files, log tailing, `_wait_for_exit`. Keeps (stronger): gate.py
   policy, worktree isolation + no-push (now ALSO a `PreToolUse` deny on `git push`),
   typed states + NeedsHumanReason, model pinning, resume-for-fix, one-review-pass
   routing, rules/acceptance, board UX. Billing verified: SDK rides the Pro
   subscription via `claude login` OAuth — `ANTHROPIC_API_KEY` must never be set.
2. **Supervisor lives on the homelab NAS** (24/7). Repos, git creds, claude/codex
   logins live there; laptop/phone are browsers. AFK becomes real.
3. **stdlib-only is dropped** (D8 partially superseded). uv-managed venv;
   claude-agent-sdk + a real micro-framework. Work-Windows portability is solved when
   a work deployment actually exists.
4. **Lanes v1: Claude workers + Codex reviewer** (codex SDK, ChatGPT-plan billing),
   behind a narrow Lane protocol (spawn/resume/stream/cost only). Full worker parity
   is a LATER, bounded addition, gated on (a) the loop being boring and (b) measured
   — not guessed — quota pressure.
5. **Per-repo config is central** (D17 pattern generalized): registry in the cox home,
   agent-scouted on first contact with a repo (discover test/lint/build from
   package.json/pyproject/CI), human-editable, NOTHING committed to target repos.
   `.cox/repo.yml` is retired. Gate rule change: missing commands on a `full` task =
   RED, never silent "skip" (the gate must not lie).
6. **Approvals: merge-only by default.** Plans auto-approved; the ONE standing human
   gate is merge (zero-unreviewed-merges stays locked). Plan-approval is opt-in via
   the high-scrutiny preset for big/ambiguous tasks.
7. **Ratchet enforcement: this contract.** The assistant refuses to build any new
   surface until §3 is met, unless the captain explicitly says "override the
   contract". Banned until then: new dashboard features, lane parity, autonomy
   presets, models-refresh skill, Telegram, work-port activity.
8. **Same repo.** History and evidence culture (SHAKEDOWN/DECISIONS) keep accruing;
   transport modules are deleted in the same PRs that replace them.

Carry-over fixes folded into the rebuild (from the Run B forensics):
- Never sum cached tokens into input (both lanes) — report fresh vs cached.
- An infra-error review result is NEVER cached as a verdict (P2 applies to completed
  reviews only).
- Pre-flight the quota window before spawning a paid session; don't launch a reviewer
  into a 97%-utilized window.
- Timeouts must be distinguishable from exits, and orphans must be killed.

## 3. Definition of done (falsifiable — the 30-day test)

v3.5 is DONE and attempt 3 is a SUCCESS when the remaining MoneyPulse #98 backlog
(~9 sync-hardening issues) has landed end-to-end through the new loop:
- dispatched from the board (phone or laptop),
- **≤1 manual unstick across the entire backlog**,
- captain touched only optional plan-edits and merges,
- zero unreviewed merges; zero untyped needs-human,
- the board never reported fiction (state matches reality at every check),
- one crash test passed: kill `coxd` mid-task; the task resumes and ships.

## 4. Build slice (the ONLY sanctioned work until §3)

**Week 1 — the loop, no UI.**
1. ✅ **Billing spike — PASS 2026-07-14** (`coxd/spike_billing.py`, coxd/SPIKE-1-BILLING.md).
   claude-agent-sdk 0.2.119 ran a haiku session on the subscription (no API key);
   structured `ResultMessage` usage proves the cached-token double-count bug is now
   impossible. SDK API confirmed: `query()`/`ClaudeSDKClient`, `ClaudeAgentOptions`
   (model/resume/cwd/permission_mode/hooks/max_budget_usd), native session mgmt +
   `RateLimitEvent` (the quota pre-flight we lacked).
2. `coxd` runs ONE task end-to-end: dispatch → worktree → SDK session (typed event
   stream) → deterministic gate (registry commands) → codex-SDK review →
   needs-you notification → resume-for-fix. Observed via a CLI tail over the event
   store. Stub-lane e2e ports over for CI.
   PRIMITIVES PROVEN (2026-07-14):
   - ✅ 2a worker (`coxd/spike_worker.py`): ClaudeSDKClient session in an isolated
     cwd; `PreToolUse` hook hard-denies `git push` (replaces `_worker_env`);
     structured cost/session — no log parsing.
   - ✅ 2b registry+gate (`coxd/registry.py`, `coxd/spike_gate.py`): per-repo
     commands auto-scouted from the repo's own manifests (no `.cox/repo.yml`);
     gate is honest (missing test on a full task = RED, not silent skip — the #99
     "gate lied" defect fixed).
   - ✅ 2c review (`coxd/spike_review.py`): cross-model reviewer, clean structured
     verdict, `RateLimitEvent` surfaced (pre-flight), infra-error typed+retryable
     (never cached as a verdict — the $0.61 Run-B loss fixed).
   - ✅ 2d ASSEMBLED loop (`coxd/store.py`, `gate.py`, `lane.py`, `loop.py`,
     `spike_loop.py`): one task = one async fn, implement→honest gate→(resumed fix)
     →one review→pr_ready, with coxd the SOLE state owner (SQLite) + event log.
     Proven on a real task ($0.078). Native `resume=session_id` for fix.
   REMAINING (week 2): codex-SDK reviewer swap; needs-you notification (ntfy/
   Telegram); ✅ the stateless board (coxd/board.py, Starlette+SSE over the store); 3 concurrent
   tasks; deploy `coxd` on the NAS; port the stub-lane e2e for CI.

**Week 2 — durability + the thin board.**
3. ✅ **Live dispatch + supervisor (2026-07-15)** — `coxd/worktree.py`, `dispatch.py`,
   `supervisor.py`, `cli.py`. `coxd serve` = one process serving the board AND
   running queued tasks as asyncio tasks (concurrency cap; single store; crashes
   contained). Proven live: `coxd dispatch` ran a real task end-to-end (worker →
   auto-scouted honest gate → review → pr_ready, $0.117) watchable on the board.
4. ✅ **Board** (`coxd/board.py`, Starlette+SSE) — stateless reader over the store;
   task list, state, stepper, cost, live event feed. REMAINING on the board:
   diff/checklist views + approve/merge buttons.
   REMAINING (week 2): needs-you notification (ntfy/Telegram); codex-SDK reviewer
   swap; deploy `coxd` on the NAS; the crash-recovery test (kill coxd mid-task →
   resume); port the stub-lane e2e for CI. Then run MoneyPulse #98's backlog → §3.
5. Central repo registry + first-contact scout; deploy coxd on the NAS.

**Then:** run MoneyPulse #98's issues through it until §3 is met. Nothing else.

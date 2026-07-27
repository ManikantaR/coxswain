# Coxswain — Roadmap

Strict milestone order (DESIGN.md P10). A milestone is DONE only when its exit
criteria are met **on real tasks**, not demos. Do not start the next milestone
before that. Task-level breakdown lives in [../TASKS.md](../TASKS.md).

> **▶ Status (2026-07-05):** M0 code-complete + **T-14 shakedown underway: run 1 of ~10 LANDED**
> (full loop, coxswain shipped its own `cox status --json` via PR #1). Six real bugs found & fixed
> live (BUG-01…06, see docs/SHAKEDOWN.md); suite 44 green. Next: runs 2–10 across ≥2 repos, incl. a
> fix-round + second-repo task. M1/M2/V1 not started. Source of truth = RESUME block atop TASKS.md.

## V0 — personal (Mac), Claude-first

### M0 — the proven loop  ← EXECUTE FIRST, everything else waits
Chat → dispatch → claude worker in worktree → deterministic gate → one review
pass → human verdict → fix round (resumed session) → PR → captain merge word →
teardown. Watcher running, wake queue durable, cost ledger live.

**Exit criteria (all required):**
- 10 real issues shipped end-to-end across ≥2 repos (smartocrprocess, relay/cox) over ~2 weeks.
- Zero unreviewed merges; zero reviewer re-runs; zero untyped needs-human.
- `cox status` shows per-task cost; median orchestration overhead per task
  visible and reviewed (target: fix round costs ≪ initial implement run,
  proving resume works).
- Orchestrator restart mid-task is a non-event (kill it, relaunch, task ships).
- Watcher dies → status banner appears; task still recoverable.
- e2e stub-lane test green in CI (no tokens).

### M1 — codex lane
Second harness through the same Lane interface. Dispatch rules can route to it;
no automatic failover.

**Exit criteria:** 3 real tasks shipped via codex lane; cost ledger captures
codex usage (or logs `unknown` loudly); a rate-limited claude task manually
redispatched to codex in one chat exchange.

### M2 — Telegram AFK pings
One-way notifier wired to watcher events (needs-human, pr-ready, ci-red, landed).

**Exit criteria:** a full task dispatched before leaving the house is merged
after returning, having been steered only by pings + one chat verdict. No ping
spam (≤1/task/10min verified in a noisy failure test).

**V0 DONE =** M0+M1+M2 exit criteria all met. Only then consider V1.

## V1 — work port (Windows, TFS/Azure DevOps, enterprise)

Priorities in order; each step is shippable alone:

1. **W1 — Windows runtime**: watcher + spawner on Windows (Python already
   portable; replace detach mechanics: `CREATE_NEW_PROCESS_GROUP`/`DETACHED_PROCESS`
   instead of POSIX double-fork; pidfile liveness via `psutil`-free tasklist
   check or `OpenProcess`). No tmux/WT dependency — workers are detached
   processes, visibility via `cox peek` (relay's Windows Terminal tab code in
   `relay_spawn.py` is reference only; cox deliberately avoids terminal-tab
   coupling).
2. **W2 — SCM adapter**: abstract `cox/scm/` (v0 `gh` impl) + `azdevops` impl:
   `az repos pr create`, work-item link, CI status via `az pipelines runs`.
   TFS on-prem variant behind the same interface (REST API, PAT auth).
3. **W3 — copilot lane** (`copilot` CLI headless; enterprise Claude seat lane =
   existing claude lane pointed at the work account).
4. **W4 — policy pack**: work profile config (no Telegram; notifications via
   Teams webhook or none; models pinned to enterprise-allowed; proxy/cert env
   passthrough; audit-friendly: cost + transcript paths in PR description).
5. **W5 — multi-repo work registry**: N repos × 1 task each; per-repo
   `.cox/repo.yml` checked into each repo (commands.test/lint, review mode,
   target branch).

**Exit criteria:** one real work item shipped end-to-end on the work machine
through Azure DevOps/TFS with a copilot or enterprise-claude worker, zero
personal-account credentials involved.

## V2 — parked ideas (do not build; revisit only after V1 ships)

- Issue-queue "orchestrator proposes" intake (watcher notices labeled issues,
  asks first). Two-way Telegram. NAS deployment. Secondmate-style domain
  supervisors. VS Code surface. Auto-dispatch. Multi-captain.

## V3.5+ — parked: multi-provider lanes (Codex, Gemini/Antigravity) + UI model routing

**Status: PARKED, not started.** Gated behind [DESIGN-V35.md](DESIGN-V35.md)'s
contract (also in TASKS.md's RESUME block): no new surface — and this is
squarely "lane parity" + "dashboard features" — until MoneyPulse #98's backlog
lands per V35 §3 with ≤1 manual unstick per task. Do not pick this up before
then without the captain explicitly saying "override the contract." Captured
here (2026-07-27) so the research isn't lost — revisit and brainstorm once #98
clears.

**Ask:** route tasks across three providers instead of Claude-only — Claude
Opus stays the planner/reviewer (unchanged), OpenAI Codex becomes the general
implementation lane, Google Gemini/Antigravity becomes a cheaper lane for
lower-stakes work — with the (scope, task-type) → (provider, model, effort)
assignment editable from the board UI at runtime, not a code/config-file edit.

**Key finding — this was already built once and deliberately dropped.** The
pre-v3.5 `cox/` package (see `cox/lanes/base.py`, `cox/lanes/codex.py`,
`cox/model.py`, `cox/models.py`, `cox/templates/dashboard.html`) had: a real
`Lane` protocol (spawn/resume/parse_result) that Claude, Codex, and a stub
implementation all satisfied; a **working, JSONL-parsing `CodexLane`** with
session resume via `codex exec resume`; **three independently-pinned model
slots per task** (plan / implement+fix / review — see DECISIONS.md D14/D15);
a JSON-catalog model list designed so new models don't need a code change;
and a dashboard with **live per-slot dropdowns** (`#d-lane`, `#d-model`,
`#d-effort`, separate plan/review pickers) — i.e. the UI-editable routing
being asked for here. The v3.5 rewrite (docs/DESIGN-V35.md, `0ec0afc`)
intentionally narrowed scope to Claude-workers + Codex-reviewer-only and never
re-ported the Lane protocol, per-slot config, or the picker UI into `coxd/`.
None of that design work needs redoing — it needs porting onto the new
Agent-SDK-based transport and re-verifying, not reinventing.

**Where `coxd/` stands today** (as of this writing): `coxd/lane.py`'s comment
"codex lane is a later swap behind this shape" is aspirational — the module is
Claude-Agent-SDK-specific throughout (imports `ClaudeSDKClient`,
`HookMatcher`, `AssistantMessage`/`ResultMessage` directly; no `Lane` protocol
exists in `coxd/`). Model selection is a single hardcoded global for the whole
supervisor process (`supervisor.serve(worker_model="claude-sonnet-5",
review_model="claude-opus-4-8")`, no CLI flag, no per-task or UI override —
`coxd/board.py`'s dispatch form has no model/lane control at all).

**External landscape (checked 2026-07-27 — this space moves weekly, re-verify
model IDs at build time):**
- **Codex — strong fit.** Official Python SDK (`pip install openai-codex`,
  github.com/openai/codex/tree/main/sdk/python) with thread resume/fork,
  per-turn token usage, and `PreToolUse`/`PostToolUse`/`PermissionRequest`
  hooks that can `deny` — a near-exact analogue of the Claude Agent SDK's
  PreToolUse hook used today for the no-`git push` boundary. Hooks are shell
  scripts reading JSON on stdin, not in-process callbacks — needs a small
  generated shim, not a closure. `codex exec --json` gives a subprocess/JSONL
  fallback if the SDK path disappoints. Current model line is a generation
  past what was originally named: `gpt-5.6-sol/terra/luna` supersede
  5.5/5.4, with reasoning effort (low…max/ultra) as a separate axis from
  model choice — model `gpt-5.6-terra` at medium effort is the reasonable
  "general coding lane" default.
- **Gemini/Antigravity — the framing needs correcting, not just the
  plumbing.** Gemini CLI is discontinued, superseded by Antigravity CLI
  (`agy`, Go rewrite, announced 2026-05-19). **Gemini 3.5 Pro does not exist
  yet** — announced, then delayed specifically over coding quality; there is
  no model ID to route "capable work" to. Google is currently positioning
  **Flash, not Pro, as the agentic-coding tier** (`gemini-3.5-flash` is
  marketed as their most-intelligent model for agentic/coding tasks; the
  Antigravity Agent API offers only Flash tiers). Two integration paths, both
  rough: the managed **Agent API** has the right primitives (hooks, resume,
  `max_total_tokens` budget) but executes in a Google-hosted remote sandbox —
  a real mismatch with coxd's local-worktree-then-PR model; the **`agy`
  CLI** stays local but has an open bug where `-p`/`--print` silently drops
  stdout under a non-TTY subprocess (exactly how coxd would spawn it) plus a
  report of orchestrator-injected hooks denying every tool call. Time-box a
  spike on those two bugs before committing to this lane at all; if still
  broken, defer it rather than fight it.
- **Routing rationale — reframe "small UI work → cheap model."** No vendor
  actually segments tiers by task genre. Route by *stakes* (can a bad diff
  reach main unreviewed?) and *token volume* (long-horizon loop vs quick
  task) instead — `gemini-3.5-flash-lite` ($0.30/$2.50 per 1M) as the cheap
  tier, `gemini-3.5-flash` as the capable tier, `gpt-5.4-mini` as Codex's
  cheap sub-lane.
- **Orchestration prior art — this is a recognized category now.**
  [claw-orchestrator](https://github.com/Enderfga/claw-orchestrator) does
  almost exactly this split (independent Planner/Coder/Reviewer engine
  selection across Claude Code, Codex, Antigravity, others) — read it before
  designing the lane interface. The ecosystem consensus is **subprocess +
  JSON event streaming per vendor CLI**, not a native SDK loop per vendor,
  because the SDKs don't share a shape — treat the Claude Agent SDK's native
  loop as the special case, not the template. LiteLLM now has cross-provider
  tool-permission guardrails and could serve as a shared spend-attribution
  chokepoint across lanes, but it unifies model *calls*, not agent
  *harnesses* — it's not the lane abstraction itself.
- **UI-configurable routing prior art:** GitLab Duo's model-selection admin
  panel (docs.gitlab.com/administration/gitlab_duo/model_selection) is close
  to an exact match — per-feature dropdown, instance-default with per-group
  override, takes effect immediately with no restart. Worth copying the
  shape: a `(scope, task_type) → (provider, model, effort)` table, hot-read
  per dispatch rather than cached at boot — mirrors this project's existing
  `~/.config/relay/models.yml` global-policy pattern, just DB-backed instead
  of file-and-restart.

**Recommended sequencing, once unparked:**
1. Build the lane abstraction subprocess-first (re-derive from
   `cox/lanes/base.py`'s old `Lane` protocol rather than starting fresh).
2. Ship the Codex lane first — highest parity, lowest risk, validates the
   abstraction against a real second vendor.
3. Time-box a Google/Antigravity spike (the two `agy` bugs above) before
   committing to that lane; defer it if still broken.
4. Build the routing table GitLab-Duo-style, reasoning effort as its own
   column, orthogonal to model choice.
5. Use LiteLLM (if at all) only as a spend-attribution layer across lanes,
   not as the lane abstraction.

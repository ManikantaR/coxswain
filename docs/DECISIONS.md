# Coxswain — Decision log

Locked 2026-07-04/05 after a three-track research pass (kunchenguid/firstmate +
no-mistakes code read; loop-engineering primary sources: Karpathy, Osmani,
Cherny, Steinberger, Huntley, Willison, Anthropic multi-agent posts, Yegge's
Gas Town; full post-mortem of ~/repo/relay and ~/repo/agent-orchestrator).
Change these only with a written reason appended here.

| # | Decision | Alternatives rejected | Why |
|---|----------|----------------------|-----|
| D1 | Orchestrator = agent session + zero-token watcher | Deterministic daemon brain (relay); hybrid triage | Daemon brains turn every edge case into a dead-end queue (relay's 8-cause needs_decision). firstmate proves agent-brain + bash-watcher costs ~0 tokens idle. |
| D2 | New repo, salvage relay parts | Evolve relay in place; pure greenfield | Relay's v1→v2 rewrite *plus bridge* showed evolving in place drags compromises along. Its worktree/models/evidence code is proven — lift it (SALVAGE.md). |
| D3 | One worker session per task, resumed for fix rounds | Fresh context per phase (ralph-style); persistent interactive worker | Cold-start-per-phase was relay's #1 token burner (each round re-read the repo). Resume keeps repo knowledge; review still gets fresh unbiased context. |
| D4 | Deterministic-first gate, ONE review pass, human verdict | Opus reviewer retry loop (relay); human-only review | Reviewer retry multiplied the most expensive model on infra flakiness. no-mistakes ships review auto-fix limit 0; "bottleneck is verification" consensus says make it cheap + evidence-based. |
| D5 | Dispatch paths inline/quick/full, orchestrator judges, captain overrides | Everything full loop; captain decides every time | "Straight prompt would've been cheaper" pain. Anthropic: multi-agent ≈15× tokens, only for decomposable high-value work; effort-scaling rules are their documented fix. |
| D6 | Chat + Telegram; NO dashboard | VS Code webview (relay); kanban board | Relay's UI verdict: "utterly not useful". Dashboards keep failing (Vibe Kanban company died; Gas Town needs 30–60s nudges); interrupt-driven chat + pings keeps winning (Cherny, firstmate). |
| D7 | Detached subprocesses + log files; no tmux/PTY | tmux windows (firstmate/relay); Windows Terminal tabs | tmux banned at work; relay's `script(1)`/tmux/zsh plumbing caused 4 launch-day platform bugs and silent hangs. |
| D8 | Python 3.11+ stdlib core | Pure bash (firstmate); Go (no-mistakes) | Must run on Windows at work with no bash. Relay's Python core is directly salvageable and pytest-able. |
| D9 | Chat dispatch only; no auto-start | Auto-dispatch labeled issues (relay); propose-first queue | AFK autonomy ≠ unattended starts. Auto-dispatch created gh polling churn + work starting unreviewed. Propose-first parked to V2. |
| D10 | No automatic lane failover | Relay's failover ladder | Its rate-limit probe was a blocking `while True` that froze fleet supervision. Rate limit → typed needs-human, captain redecides in one message. |
| D11 | Personal-first (Mac), then V1 work port | Portable-core-first; two siblings | One proven loop before a second platform (relay bled supporting 3 platforms before 1 task succeeded). Stdlib-Python choice keeps the port cheap. |
| D12 | v0 scope = M0+M1+M2, strictly ordered | M0 only | Captain wants Telegram + codex in v0; ordering guard (P10) keeps it from becoming relay's 5-surfaces-before-1-cycle. |
| D13 | Name: coxswain (CLI `cox`) | bosun, relay v3 | The one crew member who does not row - steers and calls the strokes. First choice "bosun" collides with an active same-niche project (yetidevworks/bosun, AI agent-session orchestrator); coxswain is fully clear on GitHub/PyPI/brew (verified 2026-07-05, docs/CLI-FACTS.md). |

## Context that shaped these

- **Token reality**: personal = Claude Pro + Codex Pro (flat-rate); work =
  Claude enterprise seat + Copilot. Discipline = protecting rate-limit quota
  and avoiding pathological paths (1M-context gate), not per-token dollars.
- **Scale reality**: 2–3 repos, ~1 task per repo, ≤3 concurrent workers. Coxswain
  is firstmate's pattern at 1/10 Gas Town's scale — anything heavier is
  over-engineering.
- **Relay post-mortem headlines** (evidence: relay MEMORY.md, ROADMAP.md,
  `data/smartocrprocess-12/worker.log`): cold-start every phase; unpinned
  `claude -p` → credit-gated 1M context; Opus review retry loops (≤5 passes/task);
  needs_decision dumping ground; blocking rate-limit loop; swallowed subprocess
  errors → silent 15-min hangs; framework before first proven cycle
  (first full cycle 2026-06-30, one task, after ~10 days + v1→v2 rewrite).

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
| D6 | ~~Chat + Telegram; NO dashboard~~ **SUPERSEDED 2026-07-08 → glance board is the home surface** | VS Code webview (relay); kanban board | Original: dashboards keep failing (Vibe Kanban, Gas Town). Superseded (DESIGN-VNEXT.md): captain reported "flying blind"; shipped a glance-and-alert board (stdlib http.server + SSE, not relay's failed webview) — the async-agent field converged here (Devin Kanban, Claude Code Agent View). Chat/Telegram remain complementary. |
| D7 | Detached subprocesses + log files; no tmux/PTY | tmux windows (firstmate/relay); Windows Terminal tabs | tmux banned at work; relay's `script(1)`/tmux/zsh plumbing caused 4 launch-day platform bugs and silent hangs. |
| D8 | Python 3.11+ stdlib core | Pure bash (firstmate); Go (no-mistakes) | Must run on Windows at work with no bash. Relay's Python core is directly salvageable and pytest-able. |
| D9 | Chat dispatch only; no auto-start | Auto-dispatch labeled issues (relay); propose-first queue | AFK autonomy ≠ unattended starts. Auto-dispatch created gh polling churn + work starting unreviewed. Propose-first parked to V2. |
| D10 | No automatic lane failover | Relay's failover ladder | Its rate-limit probe was a blocking `while True` that froze fleet supervision. Rate limit → typed needs-human, captain redecides in one message. |
| D11 | Personal-first (Mac), then V1 work port | Portable-core-first; two siblings | One proven loop before a second platform (relay bled supporting 3 platforms before 1 task succeeded). Stdlib-Python choice keeps the port cheap. |
| D12 | v0 scope = M0+M1+M2, strictly ordered | M0 only | Captain wants Telegram + codex in v0; ordering guard (P10) keeps it from becoming relay's 5-surfaces-before-1-cycle. |
| D13 | Name: coxswain (CLI `cox`) | bosun, relay v3 | The one crew member who does not row - steers and calls the strokes. First choice "bosun" collides with an active same-niche project (yetidevworks/bosun, AI agent-session orchestrator); coxswain is fully clear on GitHub/PyPI/brew (verified 2026-07-05, docs/CLI-FACTS.md). |
| D14 | Three model slots per task: plan / implement(+fix) / review, each pinned independently | Single model per task; per-role only (impl vs review) | Plan is a stateless `plan.md` handoff so its model is free (opus-plan → sonnet-impl); review is stateless on the diff so a *different provider* gives a real second opinion (Amp Oracle, adversarial-review). Generalises existing per-role resolution. See DESIGN-VNEXT.md. **Refined 2026-07-08 (review slot shipped):** review default stays opus (cross-model, same-provider — no second-subscription quota draw); cross-provider review is per-task on-demand, not a forced default (captain's need is *select*, not *always*). |
| D15 | implement + fix welded to ONE provider/model (no cross-provider fix) | Switch provider mid-task; escape-hatch cold restart as "fix" | Fix *resumes* the implementer session; no cross-CLI resume exists, so a cross-provider fix is a cold restart that dumps warm context + burns quota — the exact cost D3 avoids. Cold restart in the other lane is a new dispatch, not a fix. |
| D16 | Plan-approval = per-dispatch toggle, DEFAULT OFF | Always-on (Devin/Jules); never | Solo captain + async/AFK economics; always-on taxes every task and demands presence at each start. Optional gets Devin's catch-wrong-direction safety only when it pays. |
| D17 | Repo picker = curated clone-root + dedup + always-worktree, defanged clone | Free-text arbitrary-URL clone-and-run; local-only dropdown | Own repos (GitHub + Azure DevOps git-backed, ports to work). Clone non-recursive + hooks off + first-use confirm neutralises CVE-2025-48384. No general paste-any-URL box (highest blast radius; every major tool refuses it). |
| D18 | Quota surfaced per-lane; captain picks the lane (no auto-reroute) | Auto-failover to other lane on exhaustion | Reinforces D10/D9. Flat-rate: parallel drains the 5-hour window ~N× faster; Claude+Codex windows independent → lane choice = load-balancing. No surprise routing. |

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

# Coxswain — Roadmap

Strict milestone order (DESIGN.md P10). A milestone is DONE only when its exit
criteria are met **on real tasks**, not demos. Do not start the next milestone
before that. Task-level breakdown lives in [../TASKS.md](../TASKS.md).

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

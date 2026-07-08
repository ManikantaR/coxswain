# Coxswain — Task board

<!-- ▶▶ RESUME HERE — a fresh session reads THIS block first. Keep it current. ▶▶ -->
> ## ▶ Current state & next step  (updated 2026-07-05)
>
> **Done:** M0 code-complete — **T-01 … T-13** all implemented, committed, and green
> (38 tests: ruff + mypy --strict + pytest; tokenless stub-lane e2e). Repo: `~/repo/coxswain`.
>
> **NEXT STEP → T-14 (M0 shakedown) — IN PROGRESS (5 of ~10 landed).** Runs 1–5 all went
> the **full loop → LANDED** (PR [#1](https://github.com/ManikantaR/coxswain/pull/1)–[#5](https://github.com/ManikantaR/coxswain/pull/5)).
> Highlights: run 1 fixed **6 bugs**; **run 4 built + proved the CODEX LANE** (gpt-5.4,
> multi-lane routing via `cox models`; fixed BUG-07/08); **run 5 = dispatch-from-UI +
> FIX-ROUND**: added a ＋Dispatch button to `cox serve` (dispatched a task from the
> browser) AND **validated `--resume`: fix $0.40 vs implement $1.08 (~37%)** — the token
> thesis proven. Cost trend $1.95→$1.44 (sonnet-5)→~$1.1 (sonnet 4.6); codex = tokens only.
> Dashboard live on LAN with dispatch + stop + pause. Suite 60 green, all pushed.
>
> **Also shipped (captain-directed, ahead of "prove value first"):** `cox peek`
> narrated activity feed (frugal orchestrator) + **`cox serve` glance dashboard** —
> stdlib web board for desktop + phone on the home LAN (token URL, live SSE, per-task
> narrated feed, STOP + Pause). Verified rendering desktop+mobile with real run-1 data.
> 49 tests green. Deferred polish: shorten Bash paths / collapse duplicate tool lines
> in the feed; **plan-approval control** (Devin-style approve-before-implement) not yet
> built. Direction memo: `coxswain-observability-direction`.
>
> **Immediate next: runs 6–10** — still owed by the exit criteria: (a) a run in a
> **SECOND repo** (smartocr/relay) — all 5 so far were coxswain; (b) a **parallel AFK
> batch** (2–3 at once) — the regime where the token premium pays off; (c) **crash-recovery
> checks** (kill orchestrator/watcher mid-task, confirm recovery). ✅ done: fix-round/
> `--resume` (run 5), codex lane (run 4). **2026-07-08 dashboard polish:** (a) **codex
> JSONL feed now renders** — `render._events()` branches on schema (dotted codex types
> vs bare claude types), maps item.completed agent_message/command_execution/file_change
> → feed lines, turn.completed → token line (no cost); (b) **pipeline stepper on every
> card** — Code→Gate→Review→PR→Merged with pulse-animated active stage, green/red/dim
> coloring, ✓/✕/emoji icons, and a 🔁N fix-round loop counter at Review. 65 tests green.
> Known gaps: **BUG-09** fix-round cost not auto-recorded;
> **BUG-08 codex resume** still needs a real codex fix-round. Then M2 (Telegram) is the
> last unbuilt V0 piece. Harness: dashboard ＋Dispatch button, or `cox dispatch <repo>
> "<t>" [--lane codex] [--model opus:high]` → `cox gate` → `cox review` → `cox ship` → `cox merge`.
>
> **v-next design LOCKED 2026-07-08 → [docs/DESIGN-VNEXT.md](docs/DESIGN-VNEXT.md)** (decisions
> D14–D18; D6 superseded). Dispatch UX upgrade: three model slots (plan/implement+fix/review, each
> pinned; implement+fix welded to one provider, review defaults cross-provider), plan-approval as a
> per-dispatch toggle default-OFF, repo clone-picker (curated clone-root + dedup + always-worktree +
> defanged clone), glance-home+drill-in with repo→task→fix-round nesting, per-lane quota surfaced
> (captain picks, no auto-reroute). **Build slices:** (a) ✅ **repo clone-picker SHIPPED 2026-07-08** —
> cox/repos.py (clone-root COX_REPO_ROOT default ~/repo, dedup, defanged non-recursive+hooks-off clone,
> untrusted-until-confirmed), dispatch trust guard, /api/repos[/add,/trust], dashboard dropdown +
> add/clone + Trust-&-use, `cox repos` CLI. (b) ✅ **review-slot picker SHIPPED 2026-07-08** —
> TaskMeta.review_lane/review_model; review.py lane-aware (claude --permission-mode plan OR codex
> exec -s read-only), pinned per task, default opus (cross-provider on demand, not forced — D14
> refined); dispatch form reframed into IMPLEMENT/REVIEW slots; card + toast show the review slot.
> 75 tests green. (Plan phase/architect + plan-approval toggle still DEFERRED — needs new machinery.)
>
> **After M0 ships:** M1 → **T-15** (codex lane), then M2 → **T-16** (Telegram). Do NOT start
> M1/M2 until M0's exit criteria are met (strict order, DESIGN P10).
>
> **Working agreement (every session):**
> 1. When a task/subtask completes, **check its box + append a dated one-line note** here and update
>    the ROADMAP status line; commit the status change together with the code.
> 2. On finishing a **meaningful milestone** (M0/M1/M2/V1 or a big feature), **update the code
>    walkthrough** — run the `codebase-walkthrough` skill (UPDATE mode) on this repo so I can call
>    out what changed. Docmap: `docs/.walkthrough.docmap.json`.

Execution order is top-to-bottom within a milestone; milestones are strictly
ordered (ROADMAP.md). Each task is written to be completable by a mid-tier
model (Claude Sonnet) in one session: read DESIGN.md §refs + the task,
implement, make the acceptance checks pass, run `ruff check . && pytest`.

Conventions for every task:
- Read [docs/DESIGN.md](docs/DESIGN.md) §4 guardrails first; they override anything ambiguous here.
- Every subprocess call: check returncode; non-zero → typed error (never silent).
- New code gets unit tests in the same PR. `ruff check .` and `pytest -q` must be green.
- Mark the checkbox and append a one-line note (date, gotchas) under the task when done.

---

## M0 — the proven loop

> STATUS 2026-07-05: T-01..T-13 code-complete — 38 tests green (ruff + mypy + pytest, zero-token stub e2e). T-14 (10 real-task shakedown) is the remaining human-in-loop gate before M1.

### T-01 · Package + CLI skeleton
- [x] `pyproject.toml`: project `cox`, `requires-python >=3.11`, console script `cox = cox.cli:main`, deps: none (PyYAML optional extra `yaml`). Tool config: ruff (line-length 100), mypy strict for `cox/model.py`.
- [x] `cox/cli.py`: argparse with subcommands `status`, `dispatch`, `gate`, `fix`, `ship`, `merge`, `teardown`, `watch`, `await-wake`, `peek`, `pause`, `resume-ops`, `cost`. Each delegates to a module function; unknown/unimplemented ones exit 2 with "not implemented (see TASKS.md T-xx)".
- [x] `cox/home.py`: resolve COX_HOME (env `COX_HOME` > `~/cox-home`), create `state/`, `data/` on demand; `is_paused()` checks `state/PAUSED`.
- **Accept:** `pip install -e . && cox status` runs; `cox pause && cox status` prints PAUSED banner; pytest covers home resolution + paused flag.

### T-02 · Data model + typed states
- [x] `cox/model.py`: frozen dataclasses + enums. `TaskState` (queued, working, gating, fixing, pr_open, landed, failed, needs_human), `NeedsHumanReason` (gate-red, review-findings, worker-error, worker-stale, push-rejected, pr-error, ci-red, rate-limited, evidence-missing), `DispatchPath` (inline, quick, full), `TaskMeta` (id, repo, worktree, branch, lane, model, session_id, pr_url, state, reason, dispatched_at), `CostEntry` (phase, tokens_in, tokens_out, cost_usd|None, ts).
- [x] `cox/store.py`: load/save `state/<id>/meta.json` (atomic write: tmp+rename), append `data/<id>/status.log` and `state/<id>/cost.jsonl`, list tasks, next task-id `<repo>-<slug>-<yymmddHHMM>`.
- **Accept:** mypy --strict clean on model.py; round-trip test meta.json; concurrent-append test on status.log (two processes, no interleaved corruption — append with O_APPEND single write).

### T-03 · Model pinning (salvage)
- [x] `cox/models.py` per SALVAGE.md row 2. Resolution env (`COX_MODEL_IMPL`, `COX_MODEL_REVIEW`) > repo `.cox/repo.yml` > `~/.config/cox/models.yml` > defaults (impl: sonnet/medium, review: opus/medium — current names live in config, not code).
- [x] Hard rule: `resolve()` never returns an unpinned spec; config file present but unparseable (or PyYAML missing) → `CoxswainConfigError` (crash, DESIGN §4.8).
- **Accept:** tests for all 4 precedence levels + the crash cases. Copy relay's 1M-context warning comment verbatim.

### T-04 · Subprocess shim + test harness
- [x] `cox/proc.py`: single choke-point `run(cmd, cwd, env, timeout)` → `ProcResult(rc, out, err)`; raises `CoxswainProcError` on rc!=0 unless `ok_rc` passed. `spawn_detached(cmd, log_path, pid_path, env)` → POSIX double-fork/setsid (V1 swaps a Windows impl behind the same signature), stdout+stderr → log, pid file written before return.
- [x] `tests/conftest.py`: `fake_proc` fixture — patches `cox.proc.run` with a recorder that matches expected invocations (list of (cmd-prefix, result)) and fails the test on unexpected commands. This is how ALL unit tests fake git/gh/claude.
- **Accept:** spawn_detached test: spawns `sh -c 'echo hi; sleep 0.1'`, pid file exists, log contains `hi`, process reaped. fake_proc demo test included.

### T-05 · Worktrees (salvage)
- [x] `cox/worktree.py`: `create(repo_path, task_id) -> (worktree_path, branch)` under `COX_HOME/worktrees/<id>`, branch `cox/<id>` from repo's default branch (fetch first); `remove(...)` with fail-closed landed-check hook (full check in T-12, stub returns NotLanded for now). All git via `proc.run` (checked).
- **Accept:** integration test against a temp `git init` repo: create → file exists in worktree, branch correct; create failure (bad repo path) raises typed error, no half-made dirs left.

### T-06 · Brief template + dispatch (no spawn yet)
- [x] `cox/templates/brief.md` — start from relay's proven brief (SALVAGE row 6): goal, definition-of-done, allowed scope, status-line protocol (exact verbs from DESIGN §2.2, "append sparsely — every append may wake the supervisor"), evidence contract (`evidence/test-output.txt` required or `evidence/SKIP.md` with reason), "commit locally with clear messages; NEVER push; never touch files outside the worktree".
- [x] `cox/dispatch.py`: `dispatch(repo, brief_text|issue_no, path: DispatchPath)` — validates not PAUSED, worker cap (≤3), renders brief (issue mode pulls title/body via `gh issue view --json` through scm layer), creates worktree, writes brief, creates meta (state=queued). Does NOT spawn yet (T-07 wires it).
- [x] `config/dispatch.yml.example`: rules as natural-language descriptions + path, per DESIGN §2.5, with 5 seed rules.
- **Accept:** fake_proc test: dispatch --issue creates brief containing issue title, meta.json state=queued; cap test: 4th dispatch fails typed; PAUSED test.

### T-07 · Claude lane (spawn + parse + resume)
- [x] `cox/lanes/base.py`: `Lane` protocol per DESIGN §2.7, `RunResult(outcome, session_id, usage, raw)`; module constant `AGENT_RETRY_CAP = 1` with comment "no config override — DESIGN P2".
- [x] `cox/lanes/claude.py`: build argv per **docs/CLI-FACTS.md** (exact verified flags: headless print mode, pinned model, permission mode, stream-json output; resume with session id). Spawn via `proc.spawn_detached`. `parse_result` reads the stream-json log: extract session_id, final result subtype, usage/cost → append CostEntry (phase=implement|fix). Malformed stream → `RunResult(outcome="parse-error", raw=tail)` (→ needs-human, never retried).
- [x] Wire into `dispatch.py`: after T-06 steps, spawn, state→working, record pid.
- **Accept:** parse_result unit-tested against a checked-in fixture of real stream-json output (`tests/fixtures/claude-stream.jsonl` — capture one real tiny run once, scrub content); resume argv test; cost entry appended.

### T-08 · Watcher + wake queue
- [x] `cox/classify.py`: `classify(line) -> Wake|None` — actionable verbs exactly per DESIGN §2.2, pure function, mypy strict.
- [x] `cox/watch.py` (`cox watch`): 15s cycle over active tasks: new status.log lines (offset tracked in `state/<id>/watch.offset`) → classify; pid liveness (dead + no terminal status line → wake worker-exited); staleness (>900s no activity → wake worker-stale, escalate once, then hourly backoff); scheduled checks from `state/<id>/check.json` (T-11 uses for CI). Wakes appended to `state/wake-queue.jsonl` BEFORE offsets advance (crash-safe, dedupe by (task, verb, line-hash)). Heartbeat file each cycle. Honors PAUSED (observe only, no checks that hit network).
- [x] `cox await-wake [--timeout N]`: blocks until wake-queue has an undelivered entry, prints wakes as JSON lines, marks delivered (delivered flag in the jsonl entry, file rewritten atomically). This is what the orchestrator runs as its background ear.
- [x] `cox status`: table of tasks (id, repo, state[, reason], lane/model, cost-to-date, age) + LOUD banner if in-flight tasks exist and watcher heartbeat >60s stale + undelivered wake count.
- **Accept:** unit: classify table-driven for every verb + benign lines. Integration: fake task dir; append `working:` (no wake), append `done:` (wake queued once, dedupe on re-scan); kill -9 the watcher between classify and offset-advance is simulated by re-running scan — no lost wake, no dupe delivered twice.

### T-09 · Evidence + deterministic gate steps
- [x] `cox/evidence.py` per SALVAGE row 3: `check(task) -> EvidenceReport` (contract files exist, non-empty, mtime > dispatched_at; else rescue sweep + violation warning; else missing).
- [x] `cox/gate.py` steps 1–4 (DESIGN §2.4): rebase onto target (conflict → needs-human gate-red), run `commands.test` then `commands.lint` from repo's `.cox/repo.yml` (missing commands → skip with loud note in gate report — configured commands are the strong path), evidence check. Output `data/<id>/gate.json` report. Red → state=fixing + feedback file `data/<id>/feedback.md` containing failing output (truncated to last 200 lines per step).
- [x] `config/repo.yml.example` (goes into target repos as `.cox/repo.yml`): `commands: {test: ..., lint: ...}`, `review: full|none`, `target_branch: main`.
- **Accept:** temp-repo integration: failing test command → gate.json red, feedback.md has output, state=fixing; green path → proceeds; evidence-missing → needs-human(evidence-missing).

### T-10 · Review pass + fix rounds
- [x] `cox/review.py`: gate step 5. Build review prompt: diff (`git diff target...HEAD`), brief, criteria (correctness-only — "reviewers always find something; scope to correctness" per Anthropic best practices), required JSON schema. Invoke review model via claude lane ONE-SHOT (fresh session, read-only perms, review model pin). Parse `review.json`; findings routed per action: all-auto-fix → auto fix round; any ask-user → needs-human(review-findings) with findings rendered for chat; verdict=approve → proceed to ship. Parse/crash → needs-human(worker-error) + raw tail attached. **No re-run path exists in the code.**
- [x] `cox/fix.py` (`cox fix <id> [--notes "..."]`): assemble feedback (gate feedback.md and/or findings + captain notes) → `lane.resume(session_id, feedback)` → state=fixing→gating on completion wake. Fix-round counter in meta; round 3 red → needs-human(gate-red).
- **Accept:** fake-lane tests: review approve / auto-fix / ask-user / garbage-output paths; fix-round cap test; resume called with stored session_id (never a fresh session for fixes).

### T-11 · Ship: push, PR, CI check, merge
- [x] `cox/scm/base.py` + `cox/scm/github.py` (salvage row 4): `push(worktree, branch)`, `create_pr(title, body, branch) -> url`, `pr_state(url)`, `merge(url, squash=True)` — all via `gh`/`git` through proc.run, typed failures push-rejected/pr-error.
- [x] `cox ship <id>`: push → PR (body = brief summary + gate report + evidence excerpt + cost total + "🤖 dispatched via cox") → state=pr_open → write `state/<id>/check.json` (CI poll spec, backoff 600s→2h) for the watcher; ci-red wake → needs-human(ci-red).
- [x] `cox merge <id>`: verify PR mergeable + checks green → squash merge → state=landed → auto-teardown attempt (fail-closed ok).
- **Accept:** fake_proc tests for each gh failure mode → typed reason; PR body snapshot test; check.json written with backoff fields.

### T-12 · Fail-closed teardown
- [x] Complete `worktree.remove`: landed check = branch reachable from remote OR patch-id of worktree HEAD contained in merged PR head (squash-safe, firstmate rule) — implement via `git cherry`/patch-id against target branch. `--force` prints the exact commits that would be lost and requires typed `yes-discard`.
- **Accept:** temp-repo tests: merged-squash case tears down; unpushed-commit case refuses; force path prints commit list.

### T-13 · Orchestrator manual + stub-lane e2e
- [x] Finalize [ORCHESTRATOR.md](ORCHESTRATOR.md) (seed exists): wake-handling loop, recovery verbs per NeedsHumanReason, dispatch-path judgment guide, outcome-language rules, merge-word protocol. Keep it under 300 lines — it is loaded every session.
- [x] `cox/lanes/stub.py` + `tests/test_e2e.py`: stub lane's "agent" is a script that reads the brief, makes a deterministic commit + evidence in the worktree, appends `done:`. e2e: dispatch → watch cycle (invoked synchronously) → gate → stub review (canned approve) → ship against a local bare repo as remote (no gh; a `local` scm impl for tests) → merge (local) → teardown. Zero network, zero tokens, runs in CI.
- [x] `.github/workflows/ci.yml`: ruff + mypy (scoped) + pytest on push/PR.
- **Accept:** `pytest -q tests/test_e2e.py` green locally and in CI.

### T-14 · M0 shakedown (human-in-loop, not code)
- [ ] Ship 10 real issues per ROADMAP M0 exit criteria; keep a `docs/SHAKEDOWN.md` log: task, path used, cost, fix rounds, anything that hit needs-human and whether the reason/verbs were adequate.
- [ ] Review cost ledger: median fix-round cost vs implement cost (resume must show large savings); fix brief/manual text where contract violations appeared.
- **Accept:** ROADMAP M0 exit criteria checklist all ticked in SHAKEDOWN.md.

## M1 — codex lane

### T-15 · Codex lane
- [ ] `cox/lanes/codex.py` per CLI-FACTS.md (exec mode, sandbox flag, session capture; resume if supported — if headless resume is unavailable, implement `resume()` as fresh spawn with brief+feedback concatenated and set `usage_note="no-resume-lane"` so the ledger shows the difference).
- [ ] Dispatch rules + `--lane codex` override; ORCHESTRATOR.md redispatch verb for rate-limited.
- **Accept:** fixture-based parse test; e2e stub unaffected; 3 real tasks shipped (log in SHAKEDOWN.md).

## M2 — Telegram

### T-16 · Notifier
- [ ] `cox/notify.py`: Telegram sendMessage via stdlib urllib; config `~/.config/cox/telegram.yml` (token, chat_id, events allowlist). Called from watcher on wake verbs in allowlist. Rate limit: ≤1 msg/task/10min (state in `state/<id>/notify.ts`); batch overflow into "…and N more". Message format: `cox <task-id> needs you: <reason> — <one-liner>. Reply in chat: <suggested verb>`. Network failure → log, never crash watcher.
- **Accept:** unit tests with urllib mocked (rate limit, batching, failure swallow); manual live ping documented in SHAKEDOWN.md; M2 exit criteria run.

## V1 — work port (start only after V0 done; details in ROADMAP)

- [ ] T-17 W1: `proc.spawn_detached` Windows impl (DETACHED_PROCESS), pid liveness via `ctypes.OpenProcess`; CI matrix adds windows-latest for unit tests.
- [ ] T-18 W2: `cox/scm/azdevops.py` (az CLI: repos pr create / pipelines runs list; PAT via env), TFS on-prem REST variant; select via `.cox/repo.yml scm:` key.
- [ ] T-19 W3: copilot lane per CLI-FACTS.md work-section; enterprise-claude = claude lane + work profile.
- [ ] T-20 W4: work profile (`~/.config/cox/profile.yml`: notifier=none|teams-webhook, model allowlist, proxy env passthrough); PR body audit fields.
- [ ] T-21 W5: multi-repo registry `~/.config/cox/repos.yml` + `cox status --all-repos`; 1-task-per-repo default cap.
- [ ] T-22: one real work item end-to-end (ROADMAP V1 exit criteria).

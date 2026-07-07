# M0 Shakedown log

Purpose: prove the loop (dispatch → worker → gate → review → fix → PR → merge)
on **real tasks with live `claude` workers** — not demos. This is the human
gate before M1 (ROADMAP.md M0 exit criteria).

Environment: Mac, `COX_HOME=~/cox-home`, claude lane, Python 3.11 venv.

## Exit criteria checklist (all required to close M0)

- [ ] 10 real issues shipped end-to-end across ≥2 repos (smartocrprocess, relay/cox).
- [ ] Zero unreviewed merges.
- [ ] Zero reviewer re-runs.
- [ ] Zero untyped needs-human.
- [ ] `cox status` shows per-task cost.
- [ ] Median fix-round cost ≪ initial implement run (proves `--resume` works).
- [ ] Orchestrator restart mid-task is a non-event (kill watcher, relaunch, task ships).
- [ ] Watcher dies → status banner appears; task still recoverable.
- [ ] e2e stub-lane test green in CI (no tokens). ✅ (already green, 38 tests)

## Preflight (zero-token, one-time)

- [x] `COX_HOME=~/cox-home cox status` runs clean on an empty home. → `no tasks` (2026-07-05)
- [x] `cox watch --once` returns with no tasks (no crash). → `scan enqueued 0 wake(s)`
- [x] `cox pause` / `cox resume-ops` toggles the PAUSED banner. → PAUSED banner shows; resume clears it.

## Runs

| # | date | repo | task | path | lane | gate | review | fix rounds | impl $ | fix $ | outcome | notes |
|---|------|------|------|------|------|------|--------|-----------|--------|-------|---------|-------|
| 1 | 2026-07-05 | coxswain | `cox status --json` | full | claude/sonnet | PASS | approve (0 findings) | 0 | $1.61 | — | **LANDED** ([PR #1](https://github.com/ManikantaR/coxswain/pull/1)) | full loop; surfaced BUG-01…06; total $1.95 (impl $1.61 + review $0.33) |
| 2 | 2026-07-06 | coxswain | narrated-feed polish (collapse dupes + shorten paths) | full | claude/sonnet | PASS | approve (0 findings) | 0 | $1.17 | — | **LANDED** ([PR #2](https://github.com/ManikantaR/coxswain/pull/2)) | **first clean run** — all 6 fixes held: no re-dispatch, review completed (no zombie hang), merge worked first try. total $1.44. surfaced OBS-tool-friction. |

> **Run 2 = the loop actually working.** Where run 1 needed 3 dispatches + 6 fixes,
> run 2 went dispatch → worker → gate → review(approve) → ship → merge → landed on
> the first pass. Validated live: BUG-03 (cost ingest, $1.17 captured), BUG-05 (review
> completed cleanly, no 15-min zombie hang), BUG-06 (merge clean first try). Bonus: the
> shipped feature makes the cost driver *visible* — the mypy loop now renders as one
> line `→ Bash  mypy …  (x7)` instead of 7.

| 3 | 2026-07-06 | coxswain | broaden tool allowlist (mypy/python) + brief guidance | full | **claude-sonnet-4-6** | PASS | approve (0 findings) | 0 | $0.46 | — | **LANDED** ([PR #3](https://github.com/ManikantaR/coxswain/pull/3)) | first run on sonnet 4.6; **total $0.55** — ~3× cheaper than the sonnet-5 default; no tool-friction (dogfooded its own fix); watched live on `cox serve`. |

| 4 | 2026-07-07 | coxswain | `cox models` routing command | full | **codex/gpt-5.4** | PASS | approve (1 no-op note) | 0 | $? (codex: tokens only) | — | **LANDED** ([PR #4](https://github.com/ManikantaR/coxswain/pull/4)) | **first codex-lane run**; surfaced + fixed BUG-07 (worktree .git not writable) & BUG-08 (resume flags); review flagged scope-creep as informational; captain landed as-is. 1.6M in / 25.8k out. |

> **Run 4 = codex lane proven (M1/T-15 built ahead of schedule, captain-directed).** First
> real codex/gpt-5.4 task through the full loop → landed. Multi-lane model routing live:
> claude→sonnet-4.6, codex→gpt-5.4, reviewer→opus, hard tasks via `--model opus:high`.
> Codex runs on the **Codex** quota — the quota-double-dip fix we identified. Two codex-only
> bugs found + fixed (BUG-07/08). Notable: codex **scope-creeps** (refactored all cli.py
> imports + a test beyond the ask) — the correctness-only opus review *surfaced* it as an
> informational `no-op` finding without blocking, leaving the scope call to the captain
> (design working as intended). Codex reports **no dollar cost** (tokens only) → cost shows `?`.

### BUG-07 (run #4) — codex can't commit in a linked worktree (.git outside sandbox). FIXED.
A linked worktree's index.lock + objects live in the PARENT repo's shared `.git`
(git-common-dir), outside codex's `-C worktree` + `--add-dir data_dir` sandbox, so
`git commit` failed. Fix: codex lane resolves `git rev-parse --git-common-dir` and
grants it via `--add-dir`. Claude didn't hit this (different sandbox model).

### BUG-08 (run #4) — `codex exec resume` rejects exec-only flags. FIXED (needs live validation).
`resume` doesn't accept `-s`/`--add-dir`/`-C` (those are `codex exec`-only), so
resume() now passes sandbox + writable_roots via `-c` config. Not yet exercised on a
real fix round — validate when the first codex gate-red/review-fix happens.

### OBS-codex (run #4) — codex is more autonomous/scope-creepy than claude.
It refactored unrelated code + adapted a socket-binding test to its network sandbox.
All behavior-preserving and green, but broader than the brief. The correctness-only
review won't block this (by design) — it's the captain's call. Consider a tighter
"minimal diff, do not refactor unrelated code" line in the brief for codex runs.

> **FINDING — model choice is the biggest cost lever.** Run 3 (sonnet 4.6, `COX_MODEL_IMPL=claude-sonnet-4-6:medium`)
> cost **$0.55 total** vs run 1 $1.95 / run 2 $1.44 on the `sonnet` alias (→ sonnet-5).
> Task sizes differ, so it's not a clean apples-to-apples, but a ~3× gap is too big to
> ignore. **Candidate default change:** pin the implementer to sonnet 4.6 for routine
> tasks and reserve sonnet-5 for hard ones (models.yml). This directly attacks the
> token-premium worry — a cheaper implement model shrinks the whole economics question.

> **Run 1 = first full end-to-end loop, and coxswain shipped its own feature.**
> Three dispatches to get a clean worker run: #1 → BUG-01 (sandbox blocks
> status/evidence writes), #1b → BUG-02 (`--add-dir` variadic ate the brief),
> #1c → clean run. Then the back half surfaced three more: BUG-03 (cost/session
> never captured), BUG-04 (review unwired + parsed-before-done), BUG-05 (`is_alive`
> hangs on zombies), BUG-06 (`gh pr merge --delete-branch` fails on a
> worktree-held branch). **All six fixed, tested, committed, pushed.** Loop proven:
> dispatch → worker → gate → review(approve) → ship(PR #1) → merge → teardown →
> **landed**. `cox status --json` now runs live from merged main. 44 tests green.
>
> Cost note: $1.95 total for a one-flag change is high — the implementer looped
> ~9× on `mypy --strict` (73 tool calls). Watch this as runs 2–10 accumulate; it's
> the "expensive small lift" relay pattern and a candidate for a tighter brief or a
> cheaper implement model on trivial tasks.

## Observations / bugs found

### BUG-01 (2026-07-05, run #1) — worker sandbox blocks status/evidence writes. FIXED.
First live dispatch (`cox status --json`, claude/sonnet). The worker tried to
append its `working:` line to `status.log` and Claude Code refused:
> Output redirection to `…/cox-home/data/<id>/status.log` was blocked. Claude
> Code may only write to files in the allowed working directories: `…/worktrees/<id>`.

Root cause: `status.log` and the `evidence/` dir live under `COX_HOME/data/<id>/`,
**outside the worktree**. A real sandboxed `claude` can only write inside its cwd,
so the entire liveness + evidence protocol was unwritable. The stub-lane e2e never
caught this — it doesn't spawn a real sandboxed worker. Worker died `is_error:true`,
no commit, no evidence, $0.28 burned.

**Fix:** claude lane now passes `--add-dir <data_dir>` on spawn and resume
(`cox/lanes/claude.py`), granting the worker write access to its own task data dir.
Regression test `test_spawn_grants_data_dir` locks it in. `--add-dir` verified in
`claude --help`. Re-dispatch after this fix = run #1b.

### BUG-02 (2026-07-05, run #1b) — `--add-dir` is variadic; it ate the brief. FIXED.
The BUG-01 fix placed `--add-dir <data_dir> <brief>` with the brief immediately
after the dir. But `claude`'s `--add-dir <directories...>` is **variadic**: it
consumed the brief as a *second directory*, leaving no prompt. Headless with a
prompt arg it errors fast (`Error: Input must be provided … when using --print`);
the detached worker instead sat alive for ~5 min emitting zero bytes (no prompt,
nothing to do). First live run looked like a hang.

Proven with a two-arm probe: `--add-dir DIR <brief>` → rc=1 "Input must be
provided"; `--add-dir DIR --model … --verbose <brief>` → rc=0, result ok in 3.7s.

**Fix:** `--add-dir <dir>` now sits mid-argv (followed by `--model`/other flags),
so a `--flag` terminates the variadic list and the brief stays the lone trailing
positional. Regression test asserts the token after the dir starts with `--`.
Re-dispatch = run #1c.

### BUG-03 (2026-07-05, run #1c) — implement cost + session_id never captured. FIXED.
Run #1c completed cleanly (worker wrote `done:`, committed, wrote evidence), but
`cox cost <id>` showed `$0.0000 / 0 tokens`. Root cause: only `review.py` appends
to the cost ledger. The **implementer** worker's cost is in `worker.log`, but
nothing parses it after the worker exits — `dispatch` spawns *before* a session
exists, and no post-exit ingest step runs. Same gap broke fix-rounds: `fix.py`
raises `no session_id to resume` because `meta.session_id` is never populated
(the id only appears in the worker's result object).

**Fix:** `gate.ingest_worker_result(task_id)` runs at the top of `run_gate` — the
first orchestrator action after `done:`. It parses `worker.log` once, appends the
implement-phase cost, and persists the `session_id` into meta (unbreaking
`--resume`). Idempotent: guarded on an existing `implement` cost entry, so
re-gating after a fix round doesn't double-count; the fix round logs its own cost.
Test `test_ingest_worker_result_records_cost_and_session` (incl. idempotence).

Also added `.cox/repo.yml` for coxswain (`test: pytest -q`, `lint: ruff check .`)
so the gate actually runs the deterministic checks instead of skipping them.

### BUG-04 (2026-07-05, run #1 review) — review pass unwired + parsed before completion. FIXED.
No `cox review` subcommand existed, and `review.review()` spawned the reviewer
detached then parsed `review.log` immediately — before the reviewer wrote it —
always returning `worker-error`. It was implicitly a two-call design (spawn, then
re-parse on a wake) that nothing drove. **Fix:** `review()` now blocks on the
reviewer pid then parses; added `cox review <id>`. Live verdict on run 1:
`{"findings":[],"verdict":"approve"}` — clean approve, review cost $0.33 recorded.

### BUG-05 (2026-07-05, run #1 review) — `is_alive()` reports zombies as alive → wait hangs. FIXED.
The reviewer finished (result in log) but became a `Z <defunct>` zombie; `is_alive`
used `os.kill(pid, 0)`, which succeeds for un-reaped zombies, so `_wait_for_exit`
blocked ~15 min until timeout and `review.json` was never written. **Fix:**
`is_alive()` now reaps its own children (`waitpid(WNOHANG)`) and treats zombie
state (`ps -o state` = `Z`) as dead. Also added a DESIGN-P2 guard: `review()`
returns a cached `review.json` verdict instead of re-spawning (no re-run, no
double-spend) — regression test `test_review_returns_cached_verdict_without_respawn`.

### BUG-06 (2026-07-05, run #1 merge) — `gh pr merge --delete-branch` fails on worktree-held branch. FIXED.
The remote squash-merge of PR #1 succeeded, but `--delete-branch` then tried to
delete the *local* branch that the task worktree still had checked out; git
refuses, so `merge()` raised and the task stayed `pr_open`. **Fix:** dropped
`--delete-branch` (fail-closed teardown already removes the worktree then the local
branch, in the right order) and made `merge()` idempotent (already-merged =
success) so recovery is safe. Re-ran `cox merge` → **landed**, teardown clean
(patch-id match handled the squash). Minor follow-up: remote branch is left
dangling after merge (enable repo auto-delete, or add a teardown remote-branch
delete) — cosmetic.

### OBS-tool-friction (run #2) — allowlist is prefix-brittle + incomplete.
`_ALLOWED_TOOLS` = `Edit,Write,Read,Bash(git*),Bash(pytest*),Bash(npm*),Bash(ruff*)`.
Two problems seen live: (a) `mypy` isn't in the list at all, yet workers run it every
time (and loop on `mypy --strict` — the #1 cost driver in both runs); (b) prefix
patterns don't match common invocations — the worker tried `python -m pytest` and
`python3 -m mypy`, which don't match `pytest*`/(absent) `mypy`, so it burned turns
seeking approval and trying variant binaries before working around. Fix options:
broaden the allowlist (add `Bash(mypy*)`, `Bash(python*)`), or drop the allowlist
under `acceptEdits` (which doesn't seem to gate Bash consistently anyway) and rely on
the stripped push-creds + worktree sandbox for the trust boundary. Also worth telling
workers in the brief: "run `pytest`/`ruff`/`mypy` directly, not via `python -m`."

### OBS (non-blocking, run #1c)
- The worker added `pythonpath = ["."]` to `pyproject.toml` — not scope creep: it
  lets `pytest` import `cox` from a worktree that has no editable install. Kept.
- The worker ran `mypy` although `mypy` is not in `_ALLOWED_TOOLS`
  (`Edit,Write,Read,Bash(git*|pytest*|npm*|ruff*)`). Under `--permission-mode
  acceptEdits` the allowlist appears not to gate Bash — revisit whether the lane
  should pass `--disallowedTools` or tighten this for the work profile.
- Stale wakes for torn-down tasks (#…051612, #…052016) linger in
  `wake-queue.jsonl` after their state/data dirs were deleted. Teardown should
  purge a task's undelivered wakes. Low severity (they're harmless noise).

<!-- Log anything the shakedown surfaces: guardrail gaps, token surprises,
     UX friction in the chat loop, crash-recovery behavior. Each becomes a
     TASKS.md fix item if it blocks M0. -->

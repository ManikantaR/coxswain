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
|   |      |      |      |      |      |      |        |           |        |       |         |       |

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

<!-- Log anything the shakedown surfaces: guardrail gaps, token surprises,
     UX friction in the chat loop, crash-recovery behavior. Each becomes a
     TASKS.md fix item if it blocks M0. -->

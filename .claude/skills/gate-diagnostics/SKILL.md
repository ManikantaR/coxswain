---
name: gate-diagnostics
description: Self-diagnose coxd's gate and review machinery — migration-journal parity for a worktree, whether gate.diff() actually spans the full branch (not just the last commit), and per-task cost/turn history from the coxd store. Use when a coxd task parks unexpectedly, a review verdict looks wrong or too cheap/fast for the diff size, or before trusting a review outcome on a task that went through an internal fix-round.
---

# coxd gate diagnostics

Three checks that were, before this skill existed, re-typed as ad-hoc Python one-liners
every time something needed verifying mid-session. One of those ad-hoc checks is how a real
bug got found: a `review-error` on task #107 that looked like a `max_turns` problem turned
out to be `gate.diff()` silently returning a 474-byte diff instead of the real 27KB feature,
because its base defaulted to `HEAD~1` — correct only when the branch has exactly one commit.
Any internal fix-round (a second commit) broke it, and it had already done this silently on
two *already-merged* PRs' reviews before anyone noticed.

Run `scripts/diagnose.py` from this skill's directory (needs the coxd venv active or
reachable — the script inserts `coxd/`'s path itself, four levels up from the script).

## When to reach for this

- **A task parks at `needs_human/review-error`** and the reason isn't obviously an SDK/infra
  fault (rate limit, transport error). Run `diff-sanity` first — a review-error after ANY
  fix-round should make you suspicious of the diff the reviewer actually saw, not just the
  turn budget.
- **A review verdict feels too fast or too cheap for how big the change should be.** A
  genuinely large feature reviewed in 2 turns for $0.05 is a signal the reviewer saw less
  than you think it saw — check `diff-sanity`.
- **Before trusting an `approve` on a task that went through 2+ commits** (any internal
  fix-round). Don't assume the fix-round automatically means the reviewer re-examined the
  whole feature — verify it.
- **A migration-related gate failure** (or one you want to preempt before shipping) — run
  `migrations` directly against a worktree instead of hand-writing the check again.
- **Sanity-checking cost/effort tuning** — `cost` shows the store's per-task total plus, when
  filtered to one task, the per-stage `result` events (cost, turns, stop_reason) so you can
  see exactly where the money went instead of trusting only the final total.

## Commands

```
python3 scripts/diagnose.py migrations <worktree-path>
python3 scripts/diagnose.py diff-sanity <worktree-path>
python3 scripts/diagnose.py cost [task-id-substring]
python3 scripts/diagnose.py all <worktree-path> [task-id-substring]
```

`COXD_HOME` should be set the same way it is for any other coxd invocation (defaults to
`~/.coxswain`).

## Reading `diff-sanity`'s output

It compares `gate.diff()`'s file count against a direct `git diff --stat <base>...HEAD` using
the *same* resolved base, and reports commits-since-base. A mismatch, or a file count that
looks too small given the commit count, is the same failure class as the #107 bug — even if
the specific `HEAD~1` default has since been fixed in `gate.py`, this check exists precisely
so a *regression* of that fix (or an analogous bug introduced elsewhere in the diff-sourcing
path) gets caught by running the check, not by re-deriving the investigation from scratch.

## Report
State the check result plainly (OK / RED / MISMATCH + the specific reason). Don't narrate
the script's internals unless something is actually wrong.

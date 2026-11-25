# Pinned Lead Session — Runbook

Operating manual for the long-lived lead ("firstmate") session driving `coxd`.

## Purpose

The lead talks to the captain, plans work, writes and refines GitHub issues, dispatches tasks **only when the captain asks**, reviews PRs and diffs, resumes or retries stuck tasks, and reports outcomes.

**The lead does not write project code.** Disposable worker agents inside `coxd` implement changes in isolated git worktrees.

## Pinning the session

Start a long-lived Claude Code session in the `coxswain` repository so it loads `CLAUDE.md` and the `/coxd` skill ([plugins/coxd/skills/coxd/SKILL.md](../plugins/coxd/skills/coxd/SKILL.md)):

- **Model and effort:** run on the strongest reasoning model at max effort (matching `/coxd`).
- **After a restart or context reset (re-pinning):**
  1. Re-read each active repo's status file via `.venv/bin/python cli.py status --repo <repo>` (from `coxd/`).
  2. Run `.venv/bin/python cli.py list` to inspect all tasks in the store and their current states.
  3. Check any open PRs or `needs_human` tasks before taking action.

## What it reads

### 1. Per-repo STATUS files (primary check — avoid polling the board)

`coxd/status.py` writes a concise status file to `$COXD_HOME/status/<repo>.md` (never committed to target repos). It is refreshed automatically on state changes, merges, and deploys, and periodically by `watchdog.refresh_all()`.

Read it via the CLI rather than polling the board:

```bash
cd coxd && .venv/bin/python cli.py status --repo acme-widgets
```

Each `$COXD_HOME/status/<repo>.md` contains:

- `## Open (<n>)` — non-archived tasks in `queued`, `running`, or `waiting_quota`
- `## Ready to merge (<n>)` — non-archived `pr_ready` tasks with PR URL and `tier=<greenlight_tier>`
- `## Stuck (<n>)` — non-archived `needs_human` or `failed` tasks with reason and `next:` guidance
- Summary footer:
  - `Last deploy` (UTC timestamp of newest `deployed` event, or `never`)
  - `Merge level` (latest greenlight tier from active tasks, or `n/a`)
  - `Clean streak` (repo clean-merge count from `store.get_clean_streak`)

`coxd/watchdog.py` also generates a once-daily morning digest (`COXD_DIGEST_AT`, default `07:20` in `COXD_TZ`, tracked via `$COXD_HOME/digest-YYYY-MM-DD`) grouped by repo: Ready to merge, Needs you (with triage next steps), Running, Waiting for usage window, Landed in the last 24 h, Deploys since last digest, Merge-level (`greenlight`) changes since last digest, PII denylist health (when not `ok`), 24 h cost, and month-to-date spend.

### 2. Board endpoints and CLI inspection

When deeper detail is needed than `STATUS` provides:

- `GET /api/tasks` (`?all=1` to include archived tasks) — full JSON task list, `agy_queue`, `factory`, `intake`, `pii`, and `spend` summary.
- `GET /api/task/<id>/events` — the task's latest 60 events (`kind`, `data`, `ts`).
- `.venv/bin/python cli.py list` — `<state> <id> $<cost>` summary across the store.
- `.venv/bin/python cli.py tail <task_id>` — stream task events until a terminal state (`pr_ready`, `landed`, `needs_human`, `failed`).

Note on `POST` board calls (`/api/dispatch`, `/api/task/<id>/retry`, `/api/task/<id>/resume`, `/api/task/<id>/stop`): when `COXD_OWNER_USER` is set, every `POST` requires either Traefik forward-auth headers (`X-Coxd-Proxy-Secret` + `Remote-User`) or `X-Coxd-Admin-Token` (no loopback exemption). The lead session must **never** read or display `$COXD_HOME/admin.token` (see [May not do](#may-not-do)); use `cli.py` or let the captain perform authenticated board actions.

### 3. Contract and decisions

- `docs/DESIGN-V35.md` — control-plane contract and architecture.
- `docs/DECISIONS.md` — locked decisions (including D19–D29).
- `CLAUDE.md` — repo context and owner-approved roadmap boundaries.

## May do

- Write, refine, and triage GitHub issues (for `coxd-agy` issues with `Depends on #N`, add the label only after `#N` has merged).
- Dispatch tasks **only when the captain requests it** (`cli.py dispatch <repo_path> "<brief>"` or `POST /api/dispatch`).
- Review worker diffs (`git -C $COXD_HOME/worktrees/<task_id> diff origin/main...HEAD`) and open PRs.
- **Resume** a `needs_human` or resumable `pr_ready` task with a targeted note (`POST /api/task/<id>/resume` with `{"note": "..."}`) when commits or session context already exist.
- **Retry** a `needs_human` task from scratch (`POST /api/task/<id>/retry`) only when no real commits landed yet (e.g., an early infra/auth failure).
- **Stop** an active task in place (`POST /api/task/<id>/stop`); committed work is kept and the task lands at `needs_human` (`stopped-by-user`).
- Report concise outcomes to the captain (e.g., `PR ready: https://example.invalid/acme/widgets/pull/12 — gate green, 0 findings, $0.42`).

## May not do

- **Never merge without the captain's explicit word**, and never enable or perform auto-merge before a repo has earned `auto` (per D29: `manual` → `proposed` at a clean streak of 10 → `auto` only via the board's **Approve auto-merge** action).
- **Never deploy migrations unattended.** Post-merge deploys touching `deploy.migration_dirs` (e.g., `migrations`, `alembic`) are held as `deploy-held` until a human deploys them.
- **Never read secrets** (`$COXD_HOME/admin.token`, `deploy/.env`, tokens, or credentials) and **never weaken** the PII gate (`coxd/pii.py`, `coxd/pii_health.py`), `coxd/envscrub.py`, or `coxd/sandbox.py`.
- **Never add `coxd` surface the owner has not approved.** The `§4` freeze is lifted (`DECISIONS.md` D27), permitting owner-approved roadmap work, while unapproved surface remains out of bounds.

## Escalation (`needs_human` reasons)

`STATUS/<repo>.md` prints a `next:` line for every stuck task using the `_NEXT` table and fallback rules in `coxd/status.py`:

| Reason / state (`status.py`) | Next step |
|---|---|
| `orphaned-on-restart` | PR may exist: check it, then Resume or archive on the board |
| `stopped-by-user` | Resume from the board if still wanted, else archive |
| `coxd-error` | Read the task events; Resume once the cause is fixed |
| Other `needs_human` (fallback) | Read the task events, then Resume with guidance or archive |
| `failed` (fallback) | Read the task events; re-dispatch with a sharper brief |

Additional `needs_human` reasons triaged in `coxd/triage.py` (used in alerts and the morning digest):

| Reason (`triage.py`) | Auto-retry? | Lead action |
|---|---|---|
| `worker-error` | Once after ~10 min (if no PR) | Wait for auto-retry; if it still fails, inspect events and Resume with a note |
| `review-error` | Once after ~10 min (if no PR) | Wait for auto-retry; if it still fails, Resume to re-run review |
| `worker-stalled` | No | Read feedback and worktree diff; Resume with a more specific note or finish by hand |
| `gate-red` | No | Read failing gate step in events; Resume with a note on how to fix it |
| `review-findings` | No | Read review findings in events; Resume with a note on which findings to fix |
| `push-rejected` | No | Check branch/remote permissions, resolve the push block, then Resume |
| `pr-error` | No | Verify `gh` auth and pushed branch, then Resume to open the PR |
| `pii-block` | No | Check flagged rule and location in events; fix the text or update the allowlist |
| `pii-scan-error` | No | Check scanner / denylist setup (`pii_health`), then Resume |

## Test data rule

All examples, fixtures, and sample outputs in lead docs and issue briefs must use invented repository names, URLs, task IDs, and numbers (such as `acme-widgets`, `acme-widgets-add-cache-1700000000`, `https://example.invalid/acme/widgets/pull/7`) — never real people's names or live data.

# tools/human — scripts the human runs

These are run by **you, on your own machine**. coxd never calls, spawns, schedules or
wraps them, and never touches `agy` or its credentials. The only interface between coxd
and agy is git branches plus the board API (`/api/task/<id>/plan`) that your own script reads.

For lead queue-ordering and model-sizing rules (`size:S` / `size:M`), see
`plugins/coxd/skills/coxd/SKILL.md` ("agy queue: lead ordering rules").

## agy-next

Install: `ln -sf "$PWD/tools/human/agy-next" ~/.local/bin/agy-next`

`export COXD_URL=http://<board-host>:<port>` in your shell profile (required; exit 2 if unset), then:

    agy-next <task_id> [--model <agy-model-id>] [--no-sandbox]
    agy-next --prune [--dry-run]

Model: `--model`, else `$AGY_MODEL`, else `gemini-4-argon-high`. See `agy models` for ids
(Gemini models use the Gemini quota; Claude/GPT models inside Antigravity use a separate,
smaller allowance). The model is printed and recorded in the log.

It fetches the plan, fetches `~/repo/<repo>`, runs a quiet auto-prune (see below), creates/reuses
a worktree at `~/repo/<repo>.agy/<task_id>` on branch `coxd-agy/<task_id>`, writes the plan to a
temp file outside the worktree and runs `agy -p ... --output-format stream-json --print-timeout 30m`
in the foreground. It never passes `--dangerously-skip-permissions`.

Afterwards: if nothing changed (agy can report success falsely) it prints
"agy made no changes" and exits 1 without publishing. Otherwise leftovers are committed as
`agy: <task_id> round <n>`, `git diff --stat` is shown, and you are asked y/N before the
branch `coxd-agy/<task_id>` is pushed to origin. Token usage is printed and appended to
`~/.local/state/agy-next.log`. Requires `bash`, `curl`, `git`, `python3` (and optionally `gh`
for PR merge detection when pruning).

Env overrides: `AGY_REPO_ROOT` (default `~/repo`), `AGY_NEXT_LOG`, `AGY_NEXT_PRUNE=0` (disables
auto-prune on normal runs).

### Pruning worktrees

`agy-next --prune [--dry-run]` cleans up finished `~/repo/<repo>.agy/<task_id>` worktrees
across all `$AGY_REPO_ROOT/*.agy` directories whose main repo `$AGY_REPO_ROOT/<repo>/.git`
exists. Worktrees outside `<repo>.agy/` are never touched.

Removal rules per worktree `<repo>.agy/<id>`:
- **Eligible**: `gh pr list --head coxd-agy/<id> --state all` has a `MERGED` PR, or the board
  `/api/tasks?all=1` reports the task as `landed` or `archived`. Otherwise kept (`pr open/unknown`).
- **Clean**: kept if `git status --porcelain` is non-empty (`uncommitted changes`).
- **Inactive**: kept if `ps` shows an active `agy` or `agy-next` process referencing `<id>` or
  `<repo>.agy/<id>` (`active run`).
- **Remove**: removes the worktree (`git worktree remove --force`), deletes the local branch
  `coxd-agy/<id>`, deletes `origin/coxd-agy/<id>` if the PR was `MERGED` and the remote branch
  still exists, and runs `git worktree prune`.

`--dry-run` prints what would be removed and which remote branches would be deleted without
modifying anything. Normal `agy-next <task_id>` runs also perform an automatic quiet prune
before creating the worktree (excluding `<task_id>`, printing only when something is removed or
a remote branch is deleted, and never failing the run; set `AGY_NEXT_PRUNE=0` to disable).

### Board usage reporting

Once per confirmed publish, `agy-next` scans the saved stream (`$tmp/agy.out`) for the final
`result` event's `usage` and `duration_seconds` (falling back to `$tmp/usage.json` and
`SECONDS - run_t0` when no `result` event is present) and sends a single
`POST /api/task/<id>/agy-usage` with:

```json
{
  "input_tokens": 120000,
  "output_tokens": 30000,
  "thinking_tokens": 10000,
  "cache_read_tokens": 60000,
  "duration_seconds": 360,
  "model": "gemini-4-hydrogen-high"
}
```

- **Once per publish**: Nothing is posted if `agy` made no changes or if you decline the publish prompt.
- **Warn and continue**: The request uses `curl -m 5 --connect-timeout 3 -s -f`. If the board is
  unreachable, times out, or returns an error, `agy-next` prints a warning to stderr
  (`agy-next: warning: could not post usage to board`) and still exits `0` after pushing.
- **Board label & subscription separation**: The board renders a separate meta line on the card
  (`agy: 220k tok · 6m · ≈$1.40 (subscription)`) and a separate monthly summary line in the spend
  panel (`agy (subscription): N runs · X M tokens · ≈$Y`) with the tooltip
  `"≈ API price (paid by subscription)"`. Because `agy` runs under your subscription, its
  estimated USD is display-only and is **never** added to `spend`, `cost_ledger`, `claude_total`,
  `model_spend`, or the monthly Claude total.
- **Price table & override file**: `coxd/agyusage.py` estimates API-equivalent cost from a static
  `PRICES` table in USD per 1M tokens (including Hydrogen at `1.50` input / `7.50` output, with
  thinking tokens billed at the output rate, `cache_read` at `0.10 × input`, and a `default`
  fallback for unknown models). You can override or add model rates in
  `$COXD_HOME/agy-prices.json`; if the file is missing or malformed, `coxd` falls back to the
  built-in defaults.

## Sandbox and safety

agy runs with `--sandbox` (terminal restrictions) by default; `--no-sandbox` turns it off
for one run. Tests and builds execute repo code as you, so the allow-list is a guard rail,
not isolation: watch the run, and keep the y/N publish step.

The raw agy stream of each run is kept in `~/.local/state/agy-next/runs/` (newest 20) so a
denied command can be diagnosed after the run.

## Permissions

File access is granted per run by agy-next itself (`write_file`/`read_file` for that run's
worktree, by absolute path, via `agy --settings`); do not add file rules to the global file.
Also clear any old "always allow" approvals saved by the Antigravity app
(`~/.gemini/config/config.json` -> `userSettings.globalPermissionGrants`): they are added to
every run and can be far broader than these lists.

Merge the `permissions` allow/deny lists (read-only commands limited to paths inside the
worktree, tests/builds, git add/commit; no chaining, redirects or `$()`) from `agy-settings.example.json` into
`~/.gemini/antigravity-cli/settings.json` (create it if missing; append to existing
arrays rather than replacing them).

## Google consumer-plan rules

Antigravity on a consumer plan is for interactive use by you. Run it yourself, in the
foreground, from your own login. Do not automate it, schedule it, or wire it to coxd.

In the Antigravity app, turn **Enable Telemetry** off (Settings) before using it on
private repos.

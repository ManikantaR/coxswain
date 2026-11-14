# tools/human — scripts the human runs

These are run by **you, on your own machine**. coxd never calls, spawns, schedules or
wraps them, and never touches `agy` or its credentials. The only interface between coxd
and agy is git branches plus the board API (`/api/task/<id>/plan`) that your own script reads.

## agy-next

`export COXD_URL=http://<board-host>:<port>` in your shell profile (required; exit 2 if unset), then:

    agy-next <task_id>

It fetches the plan, fetches `~/repo/<repo>`, creates/reuses a worktree at
`~/repo/<repo>.agy/<task_id>` on branch `coxd-agy/<task_id>`, writes the plan to a temp
file outside the worktree and runs `agy -p ... --output-format json --print-timeout 1800`
in the foreground. It never passes `--dangerously-skip-permissions`.

Afterwards: if nothing changed (agy can report success falsely) it prints
"agy made no changes" and exits 1 without publishing. Otherwise leftovers are committed as
`agy: <task_id> round <n>`, `git diff --stat` is shown, and you are asked y/N before the
branch `coxd-agy/<task_id>` is pushed to origin. Token usage is printed and appended to
`~/.local/state/agy-next.log`. Requires `bash`, `curl`, `git`, `python3`.

Env overrides: `AGY_REPO_ROOT` (default `~/repo`), `AGY_NEXT_LOG`.

## Permissions

Merge the `permissions` allow/deny lists from `agy-settings.example.json` into
`~/.gemini/antigravity-cli/settings.json` (create it if missing; append to existing
arrays rather than replacing them).

## Google consumer-plan rules

Antigravity on a consumer plan is for interactive use by you. Run it yourself, in the
foreground, from your own login. Do not automate it, schedule it, or wire it to coxd.

In the Antigravity app, turn **Enable Telemetry** off (Settings) before using it on
private repos.

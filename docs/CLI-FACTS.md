# Verified CLI facts (live-tested 2026-07-05)

Verified by executing the locally installed CLIs: Claude Code 2.1.193,
codex-cli 0.142.5, Copilot CLI 1.0.68. Re-verify (bump versions here) before
building a lane on any of these; CLIs move fast.

## Claude Code headless (claude lane — T-07)

- `claude -p "<prompt>" --output-format json` → single result JSON with
  top-level `session_id`, `total_cost_usd`, `usage` (input/output/cache
  tokens), `modelUsage` (per-model breakdown), `subtype: success|...`,
  `is_error`, `num_turns`, `result` (final text).
- Streaming variant: `--output-format stream-json` (JSONL events; final event
  is the same result object). The lane parses the log file for the final
  result object.
- **Resume works headlessly**: `claude -p --resume <session_id> "<feedback>"
  --output-format json` — recalls prior context, returns the SAME session_id.
  ⚠️ Session lookup is scoped to the working directory — always run resume
  from the task's worktree (same cwd as the original spawn).
- `--fork-session` = resume into a NEW session id (don't use for fix rounds).
  `-c/--continue` = most recent session in cwd (don't rely on it; store the id).
- `--model <name>` and `--permission-mode acceptEdits` confirmed. acceptEdits
  auto-approves file edits/mkdir/mv/cp but NOT arbitrary Bash — pass
  `--allowedTools` for the commands workers need (git commit, test runners),
  or the run can abort on a permission prompt it can't show.
- Docs recommend `--bare` for scripted calls. Source: code.claude.com/docs/en/headless

## Codex CLI (codex lane — T-15)

- `codex exec --json "<prompt>"` emits JSONL; FIRST event:
  `{"type":"thread.started","thread_id":"<uuid>"}` — that's the session id.
  Final event: `{"type":"turn.completed","usage":{...}}` (token counts, **no
  cost field** — ledger stores tokens, cost=None).
- **Headless resume exists**: `codex exec resume <THREAD_ID> "<feedback>"`
  (live-verified, same thread_id continues). `--last` variant exists but has a
  known arg-parsing bug with `--json` (openai/codex#6717) — always resume by
  explicit UUID.
- Flags: `-m/--model`, `-s/--sandbox workspace-write` (use for workers),
  `-C/--cd <worktree>`, `--skip-git-repo-check`, `-o <file>` (final message
  only). `--ephemeral` writes no session file → breaks resume; never use.
- Session files (fallback): `~/.codex/sessions/YYYY/MM/DD/rollout-*-<uuid>.jsonl`.

## Copilot CLI (copilot lane — V1 T-19)

- `copilot -p "<prompt>" --allow-all-tools` is non-interactive mode
  (`--allow-all-tools` required headlessly; env `COPILOT_ALLOW_ALL`).
- `-s/--silent` (response only), `--log-dir`, `--session-id <id>` /
  `--resume[=id]` / `--continue` for sessions, `--share[=path]` dumps session
  markdown. **No result-JSON with usage** — ledger logs `usage: unknown`
  loudly (DESIGN §4.9).

## Name check (D13)

- bosun: PyPI squatted (dead 2014 package); bosun-monitor/bosun archived; BUT
  yetidevworks/bosun is an ACTIVE AI agent-session orchestrator (same niche)
  → rejected.
- **coxswain: clear** — PyPI 404, no brew formula, no same-niche GitHub project.

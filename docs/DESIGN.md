# Coxswain — Design

> The coxswain is the one crew member who does not row: they steer, set the
> rhythm, and call the strokes. You talk to the cox; the crew does the work;
> nothing ships unreviewed.

Coxswain is a chat-first orchestrator for AI coding agents. The successor to
`~/repo/relay`, built from its post-mortem (see [DECISIONS.md](DECISIONS.md) and
[SALVAGE.md](SALVAGE.md)).

---

## 1. Principles (non-negotiable — every task in TASKS.md must respect these)

| # | Principle | Concrete rule |
|---|-----------|---------------|
| P1 | **LLM tokens only where judgment pays** | Everything mechanical (spawn, poll, classify, git, push, PR, evidence collection, test/lint runs) is deterministic Python. An LLM turn happens only: (a) user chats with orchestrator, (b) worker implements, (c) exactly one review pass, (d) resumed fix round. |
| P2 | **No retry loop ever multiplies an LLM call** | Every agent invocation has retry cap **1** (i.e., zero automatic re-runs). Infra failure of any agent step → typed `needs-human` + notification. Never re-run the reviewer. |
| P3 | **All state on disk; restarts are non-events** | Orchestrator session, watcher, and workers can each die and restart without losing a task. No in-memory-only state. Durable wake queue. |
| P4 | **One session per task, resumed** | Fix rounds resume the implementer's session (`--resume`) so feedback costs the delta, not a repo re-read. Fresh context ONLY for the review pass (unbiased) and new tasks. |
| P5 | **Fail loud, fail typed** | No swallowed subprocess errors (relay's `capture_output=True`-and-ignore bug). Every `needs-human` carries a `reason` from a closed enum, never a generic bucket. |
| P6 | **Trust boundary** | Workers never hold push credentials and never push. The control plane (cox CLI, invoked by the orchestrator) pushes and files PRs. |
| P7 | **No PTY/terminal-multiplexer plumbing** | Workers are detached subprocesses writing to plain log files. No tmux, no `script(1)`, no send-keys. (Relay's four launch-day platform bugs all lived there; work machines forbid tmux anyway.) |
| P8 | **Models always pinned** | Every agent invocation passes explicit `--model` (+ effort where supported). An unpinned `claude -p` once burned relay's credits on the 1M-context path. Config resolution: env > repo `.cox/models.yml` > `~/.config/cox/models.yml` > hardcoded defaults — and **fail loud** if a config file exists but can't be parsed (relay silently degraded when PyYAML was missing). |
| P9 | **Cost ledger is mandatory, not roadmap** | Every agent invocation's usage/cost (from the CLI's JSON output) is appended to the task's cost ledger. `cox status` shows per-task token/cost totals. Relay never built this and discovered burn at the credit wall. |
| P10 | **One proven loop before any second surface** | Milestone order in ROADMAP.md is strict. No new lane/notifier/platform until the previous milestone's exit criteria are met on real tasks. |

## 2. System overview

```
 You (captain)
   │  chat (terminal: Claude Code session)
   ▼
 ┌─────────────────────────────┐     wakes (only actionable)
 │ ORCHESTRATOR (agent session)│◄────────────────┐
 │ reads ORCHESTRATOR.md       │                 │
 │ acts ONLY via `cox` CLI   │       ┌─────────┴─────────┐
 └────────────┬────────────────┘       │ WATCHER (python,   │
              │ cox dispatch/verdict │ zero tokens)       │
              ▼                        │ classifies events, │
 ┌─────────────────────────────┐       │ absorbs benign     │
 │ CONTROL PLANE (`cox` CLI) │──────►│ ones, queues wakes │
 │ spawn / gate / push / PR /  │       └─────────▲─────────┘
 │ evidence / cost / teardown  │                 │ status.log appends,
 └────────────┬────────────────┘                 │ process exits, timers
              │ detached subprocess              │
              ▼                                  │
 ┌─────────────────────────────┐                 │
 │ WORKER (claude -p / codex   │─────────────────┘
 │ exec) in disposable         │
 │ worktree, no push creds     │
 └─────────────────────────────┘
```

Five components. Each is independently restartable (P3).

### 2.1 Orchestrator (the thing you talk to)

- A normal **Claude Code session** launched in the cox home directory; its
  operating manual is [ORCHESTRATOR.md](../ORCHESTRATOR.md) (loaded as project
  CLAUDE.md context via an `@ORCHESTRATOR.md` include).
- It **never edits project code** and never runs raw `git push`/`gh pr` —
  everything goes through `cox` subcommands, which enforce the guardrails.
- Talks in outcomes ("PR ready: <url>", "needs your call: <finding>"), not
  mechanics. Merge requires the captain's explicit word.
- On wake (watcher output injected or user prompt), the loop is always:
  `cox status --wakes` → handle each wake → re-arm watcher if needed → reply.

### 2.2 Watcher (`cox watch`)

- A single long-running **Python process** (one per cox home). Zero LLM tokens.
- Event sources, checked on a cheap poll (default 15s, pure-local file/proc
  checks — no network, no `gh` calls):
  - new lines in any task's `status.log` (workers append structured lines);
  - worker process exit (pidfile + liveness);
  - staleness: no log/status activity past `stale_secs` (default 900);
  - scheduled per-task checks (e.g., PR CI poll — these MAY hit the network,
    with exponential backoff 600s → 2h cap).
- **Classification is deterministic** (`cox/classify.py`): a status line is
  *actionable* iff its verb is in `{done, failed, blocked, needs-decision,
  gate-verdict, pr-ready, ci-green, ci-red, stale, worker-exited}`. Everything
  else (`working:`, heartbeats) is absorbed silently.
- Actionable events are appended to a **durable wake queue**
  (`state/wake-queue.jsonl`) *before* detector state advances (crash-safe),
  then delivered by notifying the orchestrator (M0: the orchestrator polls the
  queue at turn start + a `cox await-wake` blocking command the orchestrator
  can run as a background task; M2 adds Telegram for AFK).
- Watcher liveness: writes `state/watcher.heartbeat` every cycle. `cox status`
  prints a loud banner if tasks are in flight and the heartbeat is stale
  (firstmate's "no turn ends blind" guard, in deterministic form).

### 2.3 Worker lifecycle (the core loop)

Task states (closed enum, `cox/model.py`):

```
queued → working → gating → fixing → gating → pr-open → landed
                     │                            │
                     └──► needs-human(reason) ◄───┘        failed
```

`needs-human` **reasons are typed** (P5): `gate-red`, `review-findings`,
`worker-error`, `worker-stale`, `push-rejected`, `pr-error`, `ci-red`,
`rate-limited`, `evidence-missing`. Each reason has its own recovery verbs in
ORCHESTRATOR.md — this is the anti-"dumping ground" design.

**Dispatch** (`cox dispatch <repo> "<brief>"` or `--issue N`):
1. Resolve dispatch path (§2.5): `inline` | `quick` | `full`.
2. Create disposable worktree + branch `cox/<task-id>` (lift from relay
   `py/relay_spawn.py:122-126`; **check every return code**, P5).
3. Write `data/<task-id>/brief.md` from template (goal, definition of done,
   status-line protocol, evidence contract, "commit but never push").
4. Spawn worker **detached** (P7): `claude -p "$(brief)" --model <pinned>
   --permission-mode acceptEdits --output-format stream-json` with
   stdout/stderr → `data/<task-id>/worker.log`, pid → `state/<id>/pid`.
5. Parse the stream to capture **session_id** into `state/<id>/meta.json` and
   final usage into `state/<id>/cost.jsonl` (P9). (Exact flags verified in
   docs/CLI-FACTS.md.)

**Implement**: worker works in its worktree, appends sparse status lines
(`working: <phase>` at most every few minutes; `done:`/`failed:`/`blocked:`
when terminal), commits locally, writes `evidence/` (test output, screenshots
— contract in §2.6).

**Gate** (`cox gate <task-id>`) — runs on `done:`; see §2.4.

**Fix round** (`cox fix <task-id>`): on human verdict "fix with notes" or on
auto-fixable objective findings — resume the SAME session:
`claude -p --resume <session_id> "<findings + notes>" ...` (P4). Max **2** fix
rounds; the 3rd red gate → `needs-human(gate-red)`.

**Ship** (`cox ship <task-id>`): control plane pushes branch, opens PR via
`gh` (body includes evidence summary + cost total), records `pr` in meta,
schedules CI check. Captain merges by telling the orchestrator ("merge it") →
`cox merge <task-id>` (defaults `--squash`).

**Teardown** (`cox teardown <task-id>`): **fail-closed** — refuses unless the
work is provably landed (branch reachable from remote, or patch-id contained
in merged PR head, handling squash-merge; firstmate's rule). `--force` exists
but prints what would be lost.

### 2.4 Quality gate — deterministic first, human verdict

Fixed step order (not configurable, so "green" means the same thing everywhere):

```
1. rebase     deterministic  rebase onto target; conflict → needs-human(gate-red)
2. test       deterministic  commands.test from .cox/repo.yml — zero tokens
3. lint       deterministic  commands.lint — zero tokens
4. evidence   deterministic  evidence contract check (§2.6)
5. review     ONE agent pass fresh context, sees ONLY diff + brief + criteria
6. verdict    human          findings presented in chat; captain approves/annotates
```

- Steps 1–4 red → straight back to `fixing` (resumed session, P4) with the
  failing output as the feedback; **no review pass runs on a red baseline**
  (never pay for judgment on code that doesn't pass its own tests).
- Review pass (step 5): pinned strong model, read-only, writes
  `data/<id>/review.json`: `{findings: [{severity, action: auto-fix|ask-user|no-op,
  summary, file, line}], verdict: approve|fix|reject}`. Runs **once**. Parse
  failure or crash → `needs-human(worker-error)` with the raw output attached.
  **Never re-run** (P2 — this loop was relay's #1 token burner).
- `auto-fix` findings (objective: broken import, missed lint suppression) → one
  resumed fix round without bothering the captain. `ask-user` findings → chat
  (+Telegram in M2) with the finding text; captain replies approve / fix-with-
  notes / reject.
- Per-repo `review: none` mode exists for docs-only repos (delivery modes like
  firstmate's), but default is on.

### 2.5 Dispatch paths — the fast path (anti-"straight prompt was cheaper")

`config/dispatch.yml` rules map task descriptions to a path; the orchestrator
applies judgment to match, the CLI only validates. Captain override words win
(`"just do it"` → inline; `"ship it properly"` → full).

| Path | What happens | Cost shape | Example |
|------|--------------|-----------|---------|
| `inline` | Orchestrator answers/does it in-session. No spawn, no worktree. | 1 turn | "what does relay_models.py do?", typo fix in a doc |
| `quick` | One worker, worktree, deterministic gate (steps 1–4), **no review pass**, PR marked `[quick]`. | 1 worker run | bump a dependency, add a log line, rename |
| `full` | Everything in §2.3–2.4. | worker + 1 review + ≤2 fixes | features, bug fixes, anything touching logic |

Default when no rule matches: `full` for issues, ask for ad-hoc briefs.

### 2.6 Evidence contract

Workers must produce `data/<id>/evidence/` containing at minimum
`test-output.txt` (or `evidence/SKIP.md` explaining why). The brief template
states exact filenames; the gate's evidence step checks existence + non-empty
+ recency (mtime after worker start). A fuzzy rescue sweep (relay
`relay_control.py:152-175`) is kept as a fallback but logs a contract-violation
warning — if the sweep fires often, fix the brief template, don't normalize it.
PR bodies embed the evidence summary so review is "read evidence" not "re-run
everything".

### 2.7 Lanes (agent harnesses)

A lane = adapter in `cox/lanes/` implementing one small interface:

```python
class Lane(Protocol):
    name: str
    def spawn(self, brief: Path, worktree: Path, model: ModelSpec) -> SpawnHandle: ...
    def resume(self, session_id: str, feedback: str, worktree: Path) -> SpawnHandle: ...
    def parse_result(self, log: Path) -> RunResult:  # session_id, usage, outcome
```

- M0: `claude` lane only. M1: `codex` lane (verify resume support in
  docs/CLI-FACTS.md; if codex can't resume headlessly, its fix rounds fall back
  to fresh-context-with-rich-brief and the ledger will show the difference).
- V1 (work): `copilot` lane.
- **No automatic lane failover** (relay's rate-limit failover ladder + blocking
  probe loop froze the whole fleet). `rate-limited` → `needs-human(rate-limited)`
  + notification; the captain decides "wait" or "redispatch on codex".

### 2.8 Notifications (M2)

`cox/notify.py` → Telegram bot sendMessage (stdlib urllib, bot token + chat
id from `~/.config/cox/telegram.yml`). Fired by the watcher ONLY for:
`needs-human(*)`, `pr-ready`, `ci-red`, `landed` (configurable set). Message =
task id + reason + one-line context + what word to reply with. Batched: max 1
message per task per 10 min. Telegram is one-way in v0 (pings only); replying
happens in the orchestrator chat.

### 2.9 State layout (all under the cox home, gitignored)

```
state/
  watcher.heartbeat        wake-queue.jsonl        PAUSED   (kill switch: touch to halt dispatch+watcher actions)
  <task-id>/  meta.json    (repo, worktree, branch, lane, model, session_id, pr, state, dispatched_at)
              pid          cost.jsonl (one line per agent invocation: tokens in/out, cost, phase)
data/
  <task-id>/  brief.md     worker.log    status.log (append-only)    review.json    evidence/
```

Task ids: `<repo>-<issue|slug>-<yymmddHHMM>` (repo-qualified — relay pattern
worth keeping).

## 3. Tech stack

- **Python 3.11+, stdlib only** for the core (relay proved this works; ports to
  Windows for V1 with no bash dependency). Optional PyYAML with **loud** failure
  if configs exist but it's missing (P8).
- `cox` CLI via `argparse` + console_script entry point.
- External processes: `git`, `gh` (v0), `claude`, `codex`. V1 adds `az repos`/TFS.
- **Lint**: `ruff` (line-length 100). **Types**: `mypy --strict` on `cox/model.py`
  + `cox/classify.py` at minimum. **Tests**: `pytest`; unit tests fake
  subprocesses via a recorded-invocation shim (see TASKS.md T-04); one e2e test
  drives a full loop against a throwaway local git repo with a **stub lane**
  (a fake "agent" script that writes commits/evidence deterministically) — the
  loop must be provable in CI without spending tokens.
- Coxswain's own repo ships through its own gate once M0 works (dogfood), but not
  before (P10).

## 4. Guardrails summary (enforced in code, not prose)

1. Retry cap 1 on every agent invocation (P2) — constant in `cox/lanes/base.py`, no config override.
2. Max 2 fix rounds per task; 3rd red → needs-human.
3. Max concurrent workers: 3 (config, hard cap 5).
4. Every subprocess call checks returncode; non-zero → typed failure (P5). `subprocess.run(..., check=True)` or explicit handling — `capture_output` without a check is forbidden (ruff custom rule / code-review checklist).
5. Watcher never blocks on one task (relay's rate-limit `while True` froze the fleet) — all waits are per-task scheduled checks.
6. Workers get env WITHOUT `GH_TOKEN`/push creds (P6); worktree remote is set to a read-only fetch URL where possible.
7. `state/PAUSED` kill switch checked before every dispatch/spawn/ship.
8. Unpinned model = crash at spawn time, not a warning (P8).
9. Cost ledger append is part of `parse_result`; a lane that can't report usage logs `usage: unknown` loudly (P9).
10. Teardown fail-closed (§2.3).

## 5. What cox is NOT (scope fences)

- Not a daemon-brained autopilot: nothing starts without a chat dispatch.
- Not a dashboard: no webview, no VS Code extension in v0/v1. `cox status` +
  chat + Telegram pings are the whole surface.
- Not a framework: one fixed gate order, one brief template, closed state
  enums. Opinionated contracts over configurability (no-mistakes' lesson).
- Not multi-user, not a server, not NAS-deployed (parked; see ROADMAP V2 ideas).

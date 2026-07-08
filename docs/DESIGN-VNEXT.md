# Coxswain — v-next design memo (dispatch UX, model slots, repo picker)

Locked 2026-07-08 after a flow walkthrough + a web-research pass (Copilot coding
agent, Devin, Cursor cloud agents, Claude Code Agent View, OpenAI Codex, Google
Jules, Sourcegraph Amp, Aider; plus clone-time RCE and flat-rate quota sources).
This memo records **what we're building next and why**; it amends a few rows in
[DECISIONS.md](DECISIONS.md) (see end). Change only with a reason appended there.

## What triggered it

The glance dashboard shipped (stepper + codex feed, 2026-07-08). The captain then
asked three product questions and told me to research hard and grill until aligned:
1. Repo selection — dropdown of local repos, or paste a git URL that clones-and-runs?
2. Session-style UX (Claude/Copilot app) vs the current glance board?
3. Per-phase provider selection (implement / review / …)?

## The loop, unchanged

Dispatch → worktree → **implement** (commits, never pushes) → deterministic **gate**
(rebase · test · lint · evidence) → one **review** pass → fix loop (resumes the
implementer) → **ship** (orchestrator opens PR) → **you merge** → fail-closed teardown.
Human checkpoints today: dispatch (start) + merge (end), plus typed needs-human on
escalation. v-next adds one *optional* checkpoint (plan approval) and richer dispatch.

## Locked decisions

### 1. Three model slots per task

A task resolves **three** independently-pinned model slots. This generalises the
existing per-role resolution (`implementer` / `reviewer` in `cox/models.py`).

| Slot | When it runs | State | Provider may differ? | Default |
|------|--------------|-------|----------------------|---------|
| **plan** (architect) | once, up front | stateless — emits `plan.md` | **yes**, freely | opus |
| **implement** (+ fix) | fix rounds *resume* it | **stateful** — one live session | **no — welded to fix** | sonnet or codex |
| **review** | once, on the diff | stateless | **yes**, freely | opus (see note) |

> Review-default refinement (2026-07-08, shipped): D14 proposed a *different-provider*
> default. Softened to **default = opus (cross-model, same provider — no second-
> subscription quota draw), cross-provider on demand**. The captain's stated need is
> "*select* codex for review when Claude is spent," not a forced cross-provider default
> that doubles quota use on every task. The mechanism (per-task review lane+model) ships
> now; the default policy can flip to cross-provider later once quota behaviour is
> observed. See DECISIONS.md D14.

**Why the architect split is safe** (I initially over-cautioned against it): the plan
is a **written artifact handed off as a file**, not a resumed session. The architect
runs once and is gone; fix rounds resume the *implementer*, which the architect never
touched. So opus-plan → sonnet-implement → codex-review is fully expressible and does
**not** break the resume design ([D3](DECISIONS.md)). We avoid Aider's editor-context
bug (aider issue #3287, where the editor saw only the architect's last message) by
handing the implementer the **full `plan.md`**, which is the whole point of a plan.

**Why these providers, this way** (the flat-rate economics): planning is few tokens
→ spend the premium model there; implementation is many tokens → use the cheaper
capable model; review reads only the diff → few tokens, so use a **different provider**
for a genuine second opinion. Evidence: Aider architect/editor (R1-architect +
Sonnet-editor: 64% polyglot at ~14× lower cost than o1 alone); Sourcegraph Amp runs
GPT-5.5 as an "Oracle" to review Claude's work; adversarial-review literature — the
model that wrote the code is too close to it. Review being stateless means a
provider switch there costs nothing.

**The welded pair is an invariant.** implement + fix share one live session and one
provider/model. A cross-provider fix is a *cold restart* that re-ingests the diff as
fresh input, dropping warm context and burning quota — the exact cost D3 exists to
avoid. There is no mechanism to resume a Claude session inside Codex or vice-versa
(separate CLIs, separate on-disk session stores). No escape hatch; restart-cold-in-
the-other-lane is a new *dispatch*, not a fix.

### 2. Plan-approval checkpoint — optional, default OFF

A per-dispatch toggle. Off by default; flip on for large/ambiguous work. When on,
the architect posts `plan.md` and the task pauses (a typed needs-human: plan-review)
until the captain approves, then the implementer proceeds.

Devin and Jules make plan-approval *mandatory and non-configurable* — right for team
tools where the gate is where scanners/reviewers plug in. Coxswain has a **solo
captain** and its economic case is async + parallel + AFK; always-on approval taxes
every task and demands presence at each start. Optional-default-off gets Devin's
safety exactly when it pays (catches wrong-direction work before a full
implement→gate→review loop) without throttling throughput.

### 3. Repo selection — clone-picker over a designated root

- A **designated clone-root** folder (config). New repos are cloned there.
- **Dedup**: if the repo is already cloned in the root, reuse it (no re-clone).
- **Always** operate through a git **worktree** off the local clone (never the
  primary checkout) — matches the existing worktree isolation.
- Both home and work use `git clone` (work = Azure DevOps **git-backed** repos, not
  legacy TFVC), so this ports without a new lane.

**Safety** (these are the captain's *own* repos, so this is low-risk hardening, not
paranoia): clone **non-recursive** and neutralise hooks on first clone
(`core.hooksPath` → empty), with a one-time "first use of this repo?" confirm before
any test/lint command runs. This neutralises the clone-time RCE surface
(CVE-2025-48384: a weaponised `--recursive` clone writes a malicious hook that
executes on commit/merge) without friction for trusted repos. The worker still never
gets push creds ([DESIGN P6](DESIGN.md)); a captain-held PAT does the clone/push.

Rejected: a free-text "paste any git URL → clone → run agent" box as a *general*
feature. Every major tool refuses it (Copilot/Codex/Cursor/Jules all use a picker of
pre-connected repos) because untrusted-repo clone-and-run is the single highest-blast-
radius input in the system. Our clone-picker accepts URLs but only for repos the
captain adds deliberately, defanged, into the designated root.

### 4. UI — glance-home + drill-in, nested

- **Glance board stays the home surface** (SSE cards) — the async-agent field
  converged here (Devin Kanban command center, Claude Code Agent View "one screen,
  every session, which need input", Copilot Agents panel). "Sit inside the session"
  survives only in interactive tools (Aider, Cursor Composer) — not our model.
- **Drill-in** per card = the live log tail (SSE) + full stage history. Depth on
  demand; you are not required to watch.
- **Nesting repo → task → fix-rounds** (one card per task, fix-rounds as a sub-count
  — the stepper's 🔁N). Flat lists break past ~5 agents (documented failure mode);
  grouping by repo is the fix Anthropic shipped.

### 5. Quota — surfaced, captain picks the lane (no auto-failover)

Per-lane **window-remaining / exhausted** indicator on the board. When a lane is
spent the captain manually chooses the other lane. Reinforces [D10](DECISIONS.md)
(no automatic lane failover) and [D9](DECISIONS.md) (manual dispatch only). On
flat-rate, parallel agents drain the 5-hour window ~N× faster; the scarce resource
is quota, not dollars, and Claude + Codex have independent windows — so lane choice
doubles as load-balancing. No surprise routing.

## Explicitly deferred

- Free-text arbitrary-URL clone as a first-class feature (only the curated
  clone-picker ships).
- A separate *cheaper editor* distinct from the implementer beyond the plan slot —
  the plan/implement/review trio is the whole model surface for now.
- Concurrency hard-cap — a soft warning past ~3 active implementers/lane is enough
  at current scale (2–3 repos, ≤3 concurrent, per DECISIONS.md context).
- Auto-reroute on quota exhaustion (captain picks).

## Amendments to DECISIONS.md

- **D6 superseded** — "NO dashboard" no longer holds; the glance board shipped and
  is the home surface. Reason: the captain reported "flying blind" without visibility;
  the board is glance-and-alert (not the relay VS Code webview that failed), stdlib
  http.server + SSE, and drove the observability direction memo. Chat/Telegram remain
  complementary, not replaced.
- New rows D14–D18 added there for the three-slot model, plan-approval toggle,
  clone-picker, cross-provider review default, and quota-surfaced/manual-pick.

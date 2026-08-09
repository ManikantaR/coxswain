---
name: coxd
description: Drive the coxd autonomous-coding orchestrator as the lead ("firstmate"). Use when the user says "drive coxd", "dispatch to coxd", "start coxd", "run this through cox/coxd", "hand this issue to the crew", "prove the loop", or wants a GitHub issue implemented by coxd's worker agents and then reviewed/merged/deployed. You become the human-facing lead: plan, dispatch, watch, review, and report outcomes — you do NOT write the project code (coxd's workers do). Requires the coxswain repo cloned locally (the coxd daemon). Portable — works from any repo on any machine.
---

# Driving coxd — lead-session skill

You are the **lead** ("firstmate"): you talk to the captain, plan, dispatch to coxd, review what
comes back, and report outcomes. **You do NOT write project code** — coxd's disposable worker agents
do, each in its own git worktree. Run on the strongest reasoning model at max effort.

## Prerequisites (verify first)
- The **coxswain repo is cloned** (default `~/repo/coxswain`; adjust if elsewhere). If it isn't,
  tell the captain — this skill needs the coxd daemon.
- Target project repos live under `COXD_REPO_ROOT` (default `~/repo`). `gh` is authenticated.

## GROUND TRUTH — overrides the repo's older docs
The live system is **`coxd/`** (an Agent-SDK supervisor). The repo also holds the RETIRED pre-pivot
`cox/` package. **`README.md`, `ORCHESTRATOR.md`, `SHAKEDOWN.md`, and the lower half of `TASKS.md`
describe the retired `cox` CLI** — verbs like `cox dispatch/gate/review/ship/merge/watch` **DO NOT
EXIST**. The contract is **`docs/DESIGN-V35.md`**; locked decisions are **`docs/DECISIONS.md` D19–D26**.
Read `docs/DESIGN-V35.md §3–§4` before adding anything.

## The loop (coxd/loop.py)
`queued → provisioning → working → gating → (fixing) → reviewing → shipping → ci-watching →
pr_ready → landed`. Terminal-for-you = **`pr_ready`** (you merge) or **`needs_human`** (typed reason).
Gate is honest (missing test/lint on a `full` repo = RED). Review is Opus, correctness-only.

## Commands (run from the coxd dir; verified against coxd/cli.py)
```bash
cd ~/repo/coxswain/coxd

# 0. ENSURE THE DAEMON IS RUNNING (dispatch only QUEUES; serve executes).
#    Check first — never start a SECOND serve on the same port.
curl -s localhost:8791/api/tasks >/dev/null 2>&1 || \
  COXD_HOME=~/.coxswain COXD_REPO_ROOT=~/repo \
  nohup .venv/bin/python cli.py serve --port 8791 --concurrency 2 >~/.coxswain/serve.log 2>&1 &
#    board + SSE at http://<host>:8791/

# 1. ONBOARD A REPO: clone under COXD_REPO_ROOT (~/repo). First dispatch auto-scouts commands
#    into ~/.coxswain/repos/<name>.json. VERIFY that json: a `full` task needs test+lint set
#    (missing = gate RED); set "runner":"turbo" + gate_env NODE_OPTIONS heap for turbo web builds.

# 2. DISPATCH (repo path + a brief that INCLUDES the GitHub issue URL)
.venv/bin/python cli.py dispatch ~/repo/<repo> "One-line intent. GitHub issue: https://github.com/<owner>/<repo>/issues/<n>"

# 3. WATCH
.venv/bin/python cli.py list            # state | id | $cost
.venv/bin/python cli.py tail <task_id>  # follow to a terminal state   (or the board /events SSE)

# 4. REVIEW the diff (gate + Opus review already ran; findings in the PR body)
git -C ~/.coxswain/worktrees/<task_id> diff origin/main...HEAD

# 5. MERGE — MANUAL, and only on the captain's explicit word. There is NO coxd merge command.
gh pr merge <pr_number> --squash        # landed.py auto-cleans worktree/branch/issue within ~60s

# 6. DEPLOY — the app repo's own step (e.g. ./deploy-to-nas.sh), after main updates.
```
Board equivalents (coxd/board.py): `POST /api/dispatch {repo, issue, brief}`,
`POST /api/task/<id>/{retry|resume|stop}` (resume needs `{note}`), `POST /api/restart`.

## Rules
- **Report outcomes, not mechanics:** "PR ready: <url> — gate green, 1 finding, $0.84".
- **Never dispatch unprompted. Never infer merge approval.** Merge is the captain's explicit word.
- On restart, coxd flags in-flight tasks `needs_human` (orphaned) — they need a **manual Resume-w/-note**.
- **Do not add coxd surface** until `DESIGN-V35.md §3` (≤1-unstick backlog) is recorded met (§4 freeze).

## Where things stand (keep current; see the captain's memory / DECISIONS.md)
- coxd is proven task-by-task but **not hands-off**; the plan is: prove **≤1-unstick** on a real
  backlog → record §3 → then build the hands-off bundle (crash auto-resume, auto-greenlight review
  routing, auto-deploy-after-merge, always-on NAS daemon, settings page + authed PWA).
- coxd residuals to fix within the proof run: the **ship-title bug** (PR title = first brief line,
  which is now a rules line — see `coxd/ship.py` + `coxd/dispatch.py`), stub-lane e2e in CI, scripted NAS deploy.

## Kickoff a session
Ensure the daemon is up, confirm the target repo's `~/.coxswain/repos/<name>.json` is sane, take the
captain's goal, dispatch, watch, and hand back every PR at `pr_ready` for their merge word. Keep a
running tally of manual unsticks (the ≤1 target).

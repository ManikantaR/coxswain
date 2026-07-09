# Research — loop engineering + handoff + model refresh (2026-07-09)

Two web-research passes (subagents) into how leading practitioners run coding-agent
loops, what coxswain is still missing, the handoff-between-agents pattern, and how to
keep the model catalog current. Sources cited inline. This memo is the durable record;
the actionable items are tracked in [../TASKS.md](../TASKS.md).

Scope note: everything already shipped this session is EXCLUDED from recommendations —
board stall/drift alerts, diff+plan+evidence viewer, per-lane quota burn-down pills, the
structured plan.md handoff, the JSON model-catalog overlay, Haiku-4.5 reviewer.

---

## Part 1 — Loop engineering (Karpathy, Cherny, Osmani, Steinberger)

### Per-person techniques → coxswain mapping

**Andrej Karpathy** — YC "Software Is Changing (Again)", Jun 2025
([latent.space](https://www.latent.space/p/s3), [AI21](https://www.ai21.com/blog/karpathys-leash/))
- Generation⇄verification loop; make verification *fast and easy* — loop speed governs reliability.
- Autonomy is a **slider**, not binary (Cursor Tab→⌘K→⌘I): tune autonomy to task risk.
- "Demo is `works.any()`, product is `works.all()`" — the reliability gap is the point; prefer small fully-verified diffs.
- Context = short-term memory ("anterograde amnesia"); **jagged intelligence** (great + dumb, not obvious which).

**Boris Cherny** — howborisusesclaudecode.com
([site](https://howborisusesclaudecode.com/), [The Neuron](https://www.theneuron.ai/explainer-articles/claude-code-creators-boris-cherny-and-cat-wu-explain-how-to-use-agent-loops/))
- **#1 tip:** give the agent a way to **verify its own work** — feedback loops 2–3× final quality.
- `/goal` deterministic exit conditions ("all tests in test/auth pass, lint clean") — agent self-checks until met.
- **Compounding engineering:** "write the rule, not the correction" — every mistake becomes a line in CLAUDE.md.
- Context hygiene: `/rewind` failed attempts, `/compact` with hints; context rot ~300–400k tokens.

**Addy Osmani** — good-spec / future-agentic-coding
([good-spec](https://addyosmani.com/blog/good-spec/), [future](https://addyosmani.com/blog/future-agentic-coding/))
- Six-section spec: Commands, Testing, Project Structure, Code Style, Git Workflow, **Boundaries**.
- Three-tier boundaries: ✅ Always / ⚠️ Ask-first / 🚫 Never.
- Self-verification checkpoint after implementing; context engineering (load only relevant sections; one focused task).
- Orchestrator observability: track cost, accuracy, **time-to-completion**; checkpoint/rollback.

**Peter Steinberger** — Just Talk To It
([steipete.me](https://steipete.me/posts/just-talk-to-it), [Pragmatic Engineer](https://newsletter.pragmaticengineer.com/p/the-creator-of-clawd-i-ship-code))
- Stop mid-execution freely — file changes atomic, agents resume well.
- Ask for **options before changing** on uncertainty; surface **blast radius**.
- Agents write tests after each feature; periodic dup/dead-code sweeps (`jscpd`, `knip`).
- The ~800-line agent file = "organizational scar tissue" (= Cherny's compounding rules).
- **Rejected for coxswain:** he runs 3–8 agents on *main*, no worktrees, no PRs, self-committing — fights our isolation/no-push/human-merge model. Take stop/resume + scar-tissue; reject the chaos.

### Process additions (value ÷ effort)

- **P1 — Compounding repo-rules** (HIGH/LOW): review findings + captain corrections append to a
  git-tracked AGENTS.md, injected into future implementer prompts. The clearest missing loop —
  today every review's lesson is discarded. Keep append-only + captain-approved (anti-dumping-ground).
- **P2 — Acceptance criteria + implementer self-check before the gate** (HIGH/LOW-MED): a typed
  definition-of-done authored in the plan phase; implementer self-verifies and emits ticked/failed
  items as evidence before the deterministic gate. Closes the loop twice without a 2nd review pass.
- **P3 — Mechanical stop triggers → typed needs-human** (HIGH/MED): auto-STOP (not just alert) on a
  🚫-boundary path touch, files/tool/wall-clock budget, or same-file thrash. Cheap because resume is cheap.
- **P4 — plan.md quality lint before approval** (MED/LOW): presence-check Boundaries + acceptance
  criteria + task decomposition. A lint, not an agent.
- **P5 — Context-rot-aware fix resume** (MED/MED): feed the fix round ONLY the findings (no re-dump),
  track session token-fill from the stream-json, compacted resume if near the rot line.
- **P6 — Autonomy presets per task** (MED/MED): 3 presets varying ONLY plan-gate + effort (fast /
  standard / high-scrutiny). NEVER add review passes (violates ONE-review-pass).
- **P7 — Blast-radius / task-split advisory** (MED/MED-HIGH): estimate files-touched at plan time,
  warn on oversize tasks. Advisory only — captain decides.

### Dashboard additions (stdlib + SSE)

- **D1 — Per-stage timing + fix-round/cycle-time trend** (HIGH/LOW): stage durations + a sparkline;
  which stage is the bottleneck, are fix-rounds trending up (drift).
- **D2 — Acceptance-criteria checklist on the card** (HIGH/LOW): self-verified vs gate-confirmed per item.
- **D3 — Findings→rules one-click promote** (HIGH/LOW-MED): review findings list + "promote to repo rules" button.
- **D4 — Blast-radius / boundary badge** (MED/LOW): `git diff --stat` files/±lines; flag 🚫-boundary hits.
- **D5 — Context-fill % per lane vs the ~300–400k rot line** (MED/LOW-MED): when a fix-resume would be risky.

### Rejected (fight constraints or duplicate)
Parallel-on-main / no-worktrees (Steinberger); auto-merge / auto-run (vs manual dispatch); reviewer
tournament/retry loops (vs ONE review pass); impl→review handoff note (biases the reviewer — Part 2);
auto-discovery pushing changes without approval.

**Overall ranking:** P1 ≈ D1 ≈ D2 > P2 > D3 > P3 > P4 ≈ D4 > P5 ≈ D5 > P6 > P7.

---

## Part 2 — Handoff between phase hops

The mattpocock `/handoff` skill writes a session-continuity doc to the OS temp dir, referencing
(not copying) existing artifacts, redacting secrets, suggesting next skills. It's a *cooperating-
successor* tool. coxswain's hops are not all cooperative — that distinction is the answer:

- **plan → implement: ADOPT (done).** Cooperating successor; the implementer wants the architect's
  reasoning. Keep plan.md; make "open questions" required; reference paths, don't inline code.
- **impl → review: DO NOT add a handoff note (we already don't).** The reviewer's value is *not*
  sharing the author's model. Author rationale measurably lowers defect detection — confirmation bias
  ([arXiv 2603.18740](https://arxiv.org/html/2603.18740v1)) and anchoring
  ([arXiv 2603.00539](https://arxiv.org/abs/2603.00539)). Feed the reviewer ONLY diff + original
  spec/acceptance-criteria + objective verify checklist; withhold decisions/assumptions prose. Our
  review.py already passes only diff + brief + criteria — keep it.
- **review → fix: nothing needed.** Fix resumes the session; findings are the handoff. Only improvement:
  keep review output structured (finding → location → severity → check).

---

## Part 3 — Model catalog currency

### Verified current ids (2026-07)
- **Anthropic** (pinned snapshots; dateless from 4.6-gen): Opus 4.8/4.7 · Sonnet 5/4.6/4.5 ·
  **Haiku 4.5** (no 4.6+) · Fable 5. Effort levels exposed per-model: low/medium/high/**max/xhigh**.
  ([models overview](https://platform.claude.com/docs/en/about-claude/models/overview))
- **OpenAI Codex** ([docs](https://developers.openai.com/codex/models)): **gpt-5.5** (newest) ·
  **gpt-5.4** · **gpt-5.4-mini** · gpt-5.3-codex-spark (preview). Deprecated: gpt-5.2, gpt-5.3-codex.
  - ⚠️ coxswain's earlier codex ids `gpt-5.6` and `gpt-5.5-mini` **do not exist** — corrected 2026-07-09.

### Runtime discovery (both CLIs, stdlib-parseable JSON)
- Anthropic: `ant models list` → `GET /v1/models` (id, display_name, created_at, `capabilities.effort`).
  Note the CLI binary appears to be `ant`, and it needs an API key (coxswain runs flat-rate Pro — so
  NOT at startup).
- Codex: `codex debug models` (raw) / `--bundled`.

### Recommendation: `cox-models-refresh` skill (mirrors relay-models-refresh)
Built-in CATALOG = fallback seed → `~/.config/cox/models.json` overlay = source of truth →
on-demand refresh skill that discovers via `ant models list` / `codex debug models` (curated
fallback), **shows a diff**, writes explicit pinned ids on approval, and **reconciles both
directions** (add new, remove retired). Reject startup auto-discovery (network/auth/Windows/pinning).
Run `ant models list` + `codex debug models` once on the target machine first to confirm CLI names/auth.

---

## Decisions taken (2026-07-09)
- Applied: corrected codex ids; made plan.md "open questions" required.
- Building P1 (compounding repo-rules) first; full P/D backlog + refresh skill queued in TASKS.md.
- Re-run this research periodically (models shift ~every 2 months).

# Coxswain

> The coxswain is the one crew member who doesn't row: they steer, set the
> rhythm, and call the strokes.

A chat-first orchestrator for AI coding agents (Claude Code, Codex, Copilot).
You talk to one agent session (the cox); it dispatches worker agents into
disposable git worktrees, gates their output through deterministic checks and
a single review pass, and brings you PRs. Nothing ships without your word.
Zero LLM tokens spent on supervision mechanics.

Successor to `~/repo/relay`, redesigned from its post-mortem plus research into
firstmate/no-mistakes, the ralph loop, and 2025–26 loop-engineering practice.

## Status

Scaffold. Docs are complete and implementation-ready; code is being built per
[TASKS.md](TASKS.md). Current milestone: **M0 — the proven loop**.

## Read in this order

1. [docs/DESIGN.md](docs/DESIGN.md) — architecture, principles P1–P10, guardrails
2. [docs/ROADMAP.md](docs/ROADMAP.md) — M0/M1/M2 (v0) → V1 work port, exit criteria
3. [TASKS.md](TASKS.md) — implementation tasks with acceptance criteria (Sonnet-executable)
4. [docs/DECISIONS.md](docs/DECISIONS.md) — locked decisions + rationale
5. [docs/SALVAGE.md](docs/SALVAGE.md) — what gets lifted from relay, file:line
6. [docs/CLI-FACTS.md](docs/CLI-FACTS.md) — live-verified agent-CLI flags (resume, JSON output, costs)
7. [ORCHESTRATOR.md](ORCHESTRATOR.md) — the cox's own operating manual

## The five components

Orchestrator (agent session you chat with) · Watcher (zero-token Python
process) · Control plane (`cox` CLI) · Workers (headless agents in worktrees,
no push creds) · Gate (rebase → test → lint → evidence → ONE review pass →
your verdict).

## Dev

```
pip install -e ".[dev]"
ruff check . && pytest -q
```

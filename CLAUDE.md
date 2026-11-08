# coxswain — Claude Code context

## Driving coxd (as the lead / "firstmate")
The operating manual now lives in the **`/coxd` skill** (`plugins/coxd/skills/coxd/SKILL.md`),
installable anywhere via this repo's own marketplace:

```
/plugin marketplace add ManikantaR/coxswain   # (or a local path: ~/repo/coxswain)
/plugin install coxd@coxswain
/coxd                                          # from any repo → full dispatch/watch/review/merge/deploy manual
```

That skill is the **single source of truth** for driving coxd. Use it rather than duplicating its
contents here.

## If you are DEVELOPING coxswain itself (editing this repo)
- The live system is **`coxd/`** (an Agent-SDK supervisor). The `cox/` package and
  `README.md` / `ORCHESTRATOR.md` / `SHAKEDOWN.md` describe the **RETIRED `cox` CLI** — those verbs
  (`cox dispatch/gate/review/ship/merge/watch`) do not exist. The contract is **`docs/DESIGN-V35.md`**;
  the locked decisions are **`docs/DECISIONS.md` D19–D26**.
- **The `§4` freeze is LIFTED** (`docs/DECISIONS.md` D27, 2026-08-09: §3 met on a fresh backlog
  plus the crash-resume test). New coxd surface is allowed when the owner has approved it. The
  approved factory roadmap (owner, 2026-11-07/08) is the open coxswain issues: intake, triage,
  auto-rebase, per-repo concurrency, the deployer, earned auto-merge, lead-session STATUS/digest,
  and HA/DocuPulse onboarding. Reviewers: do not flag these as freeze violations. Still out of
  bounds without an explicit owner go: anything that merges without a person (until a repo has
  earned `auto`), deploys a migration unattended, or weakens the PII gate, envscrub or sandbox.

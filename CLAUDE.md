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
- **`§4` freezes new coxd surface until `§3` is recorded met.** Do not add features (auto-greenlight
  review routing, auto-deploy, always-on NAS daemon, settings page, PWA, extra lanes) before the
  hands-off ≤1-unstick loop **plus the crash-resume test** passes on a real backlog.

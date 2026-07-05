# Salvage map — what to lift from ~/repo/relay

Relay stays untouched as reference. Lift = copy the logic into cox with
tests, fixing the noted defects. Do NOT import relay as a dependency.

| Relay source | What it is | Lift into | Fix while lifting |
|---|---|---|---|
| `py/relay_spawn.py:122-126` | Disposable worktree + branch creation | `cox/worktree.py` (T-05) | Check EVERY returncode (relay swallowed `git worktree add` failures → silent hangs). No `script(1)`/PTY/tmux code — drop `relay_spawn.py:187-233` entirely. |
| `py/relay_models.py` | Model pin resolution: env > repo yml > user yml > defaults | `cox/models.py` (T-03) | Its own header comment documents the 1M-context credit burn — keep that comment. Fix: missing PyYAML with configs present must CRASH, not silently default (`relay_models.py:24-28`). |
| `py/relay_control.py:152-175` | `_collect_evidence` fuzzy rescue sweep | `cox/evidence.py` (T-09) | Demote to fallback: contract check first (exact filenames from brief template), sweep only as rescue + loud contract-violation warning. |
| `py/relay_control.py:223-270` | Push + `gh pr create` from control plane (trust boundary) | `cox/scm/github.py` (T-11) | Keep the boundary (worker never pushes). Add typed failures `push-rejected` / `pr-error` instead of generic needs_decision. |
| `py/relay_finish.py:19-27` | Exit classification regex over log tail | replace, don't lift | Coxswain parses the lane's structured JSON result (`--output-format stream-json`), not log-tail regex. Keep only as last-resort fallback in `lanes/base.py`. |
| `data/smartocrprocess-12/brief.md` | The 2.5 KB brief that actually worked | `cox/templates/brief.md` (T-06) | Add: evidence contract filenames, status-line verb list, "one task, commit locally, never push". |
| `tests/` (111 pytest tests) | Test patterns for subprocess-heavy code | style reference for T-04 | Coxswain uses a recorded-invocation subprocess shim instead of relay's ad-hoc mocks. |
| `vscode/` extension, `relay_daemon.py`, poll loop `relay_control.py:34-37,649-655`, failover ladder + probe `relay_control.py:426-433` | — | **do not lift** | Dashboard (D6), daemon brain (D1), blocking failover (D10) are all rejected designs. |

Also reusable as reference (not code): relay's repo-qualified task-id scheme,
`~/.config/relay/models.yml` layout (cox uses `~/.config/cox/`), and the
`relay-models-refresh` skill idea (recreate for cox after M0).

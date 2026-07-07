"""Codex lane (T-15, M1). Flags verified live in docs/CLI-FACTS.md + codex 0.142.5.

Spawn: `codex exec --json -m <model> -s workspace-write --add-dir <data_dir>
-C <worktree> "<brief>"`, detached, JSONL -> log. The session id is the
`thread_id` from the first `thread.started` event; resume via
`codex exec resume <thread_id> ...`. Codex reports token usage but NO cost, so
CostEntry.cost_usd is None (logged as unknown, never hidden — DESIGN §4.8).

Running codex workers on the Codex subscription keeps them OFF the Claude quota
the orchestrator uses — the point of a second lane is quota separation, not
just redundancy.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import proc
from ..model import CostEntry, ModelSpec
from .base import RunResult, SpawnHandle

# Codex exec is non-interactive; workspace-write sandbox + never-approve keeps it
# from blocking on a prompt no one can answer (cf. the claude stdin-hang lesson).
_SANDBOX = "workspace-write"


def _git_common_dir(worktree: Path) -> str | None:
    """The parent repo's shared .git (index/objects for a LINKED worktree).

    A worktree commit writes index.lock + new objects there, OUTSIDE the
    worktree — so codex's sandbox must be granted this dir or `git commit`
    fails (shakedown BUG-07). None if it can't be resolved (then we just don't
    add it; the failure is loud, not silent)."""
    try:
        r = proc.run(
            ["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            ok_rc=(0, 128),
        )
    except proc.BosunProcError:
        return None
    out = r.out.strip()
    return out or None


class CodexLane:
    name = "codex"

    def _base_argv(self, model: ModelSpec, data_dir: Path, worktree: Path) -> list[str]:
        # --add-dir grants writable roots outside the worktree: the task data dir
        # (status.log + evidence, BUG-01) and the parent repo's shared .git so
        # `git commit` can write index.lock + objects (BUG-07). --add-dir repeats.
        # Keep the brief the lone trailing positional (BUG-02 lesson).
        argv = [
            "codex",
            "exec",
            "--json",
            "-m",
            model.model,
            "-c",
            f"model_reasoning_effort={model.effort}",
            "-c",
            "approval_policy=never",
            "-s",
            _SANDBOX,
            "--add-dir",
            str(data_dir),
        ]
        gitdir = _git_common_dir(worktree)
        if gitdir:
            argv += ["--add-dir", gitdir]
        argv += ["-C", str(worktree)]
        return argv

    def spawn(
        self, brief_path: Path, worktree: Path, model: ModelSpec, log_path: Path, pid_path: Path
    ) -> SpawnHandle:
        brief = brief_path.read_text(encoding="utf-8")
        argv = self._base_argv(model, brief_path.parent, worktree) + [brief]
        pid = proc.spawn_detached(
            argv, log_path=log_path, pid_path=pid_path, cwd=worktree, env=_worker_env()
        )
        return SpawnHandle(pid=pid, log_path=log_path)

    def resume(
        self, session_id: str, feedback: str, worktree: Path, log_path: Path, pid_path: Path
    ) -> SpawnHandle:
        # Resume by explicit thread_id — never `--last --json` (arg-parse bug,
        # openai/codex#6717, CLI-FACTS). data dir = worker.log's parent.
        # `codex exec resume` does NOT accept -s/--add-dir/-C (BUG-08 — those are
        # `codex exec`-only). Sandbox + writable roots must go through `-c` config,
        # and cwd is set via spawn_detached (we run from the worktree). Writable
        # roots = data dir (status/evidence) + parent .git (commit, BUG-07).
        data_dir = log_path.parent
        roots = [str(data_dir)]
        gitdir = _git_common_dir(worktree)
        if gitdir:
            roots.append(gitdir)
        roots_toml = "[" + ",".join(f'"{r}"' for r in roots) + "]"
        argv = [
            "codex",
            "exec",
            "resume",
            session_id,
            "--json",
            "-c",
            "approval_policy=never",
            "-c",
            f"sandbox_mode={_SANDBOX}",
            "-c",
            f"sandbox_workspace_write.writable_roots={roots_toml}",
            feedback,
        ]
        pid = proc.spawn_detached(
            argv, log_path=log_path, pid_path=pid_path, cwd=worktree, env=_worker_env()
        )
        return SpawnHandle(pid=pid, log_path=log_path)

    def parse_result(self, log_path: Path, phase: str) -> RunResult:
        return parse_codex_jsonl(log_path, phase)


def _worker_env() -> dict[str, str]:
    """Env with push credentials stripped (DESIGN P6). The control plane pushes."""
    import os

    env = dict(os.environ)
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        env.pop(key, None)
    return env


def parse_codex_jsonl(log_path: Path, phase: str) -> RunResult:
    """Extract thread_id + token usage from a codex --json JSONL log.

    Events (codex 0.142.5): `thread.started` (thread_id), `item.completed` with
    item.type `agent_message` (final text), `turn.completed` (usage). Codex
    reports no dollar cost, so cost_usd stays None. Absent/garbled -> parse-error
    (-> needs-human worker-error, never retried, DESIGN P2).
    """
    if not log_path.exists():
        return RunResult(outcome="parse-error", session_id=None, cost=None, raw_tail="(no log)")

    thread_id: str | None = None
    last_msg = ""
    usage: dict | None = None
    completed = False
    errored = False
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        t = obj.get("type")
        if t == "thread.started":
            thread_id = obj.get("thread_id")
        elif t == "item.completed":
            item = obj.get("item", {}) or {}
            if item.get("type") == "agent_message":
                last_msg = str(item.get("text", ""))
        elif t == "turn.completed":
            usage = obj.get("usage") or {}
            completed = True
        elif t in ("error", "turn.failed", "thread.error"):
            errored = True

    if thread_id is None and not completed:
        tail = "\n".join(log_path.read_text(encoding="utf-8").splitlines()[-20:])
        return RunResult(outcome="parse-error", session_id=None, cost=None, raw_tail=tail)

    cost: CostEntry | None = None
    if usage is not None:
        tokens_in = int(usage.get("input_tokens", 0)) + int(usage.get("cached_input_tokens", 0))
        tokens_out = int(usage.get("output_tokens", 0))
        tokens_out += int(usage.get("reasoning_output_tokens", 0))
        cost = CostEntry(phase=phase, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=None)
    outcome = "success" if (completed and not errored) else "failed"
    return RunResult(outcome=outcome, session_id=thread_id, cost=cost, raw_tail=last_msg)

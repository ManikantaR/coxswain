"""Run worker-controlled code as an unprivileged user (COXD_WORKER_USER).

The coxd process (control plane) runs as root: it owns the store, the registry, the
GitHub/Telegram credentials, and is the only thing that pushes. Everything the LLM can
influence (the claude CLI and its tools, the gate's test/lint/build, `pnpm install`)
runs as COXD_WORKER_USER, which can write its worktree and the shared git object
store but cannot read the store, /root or another uid's /proc/<pid>/environ (where
GH_TOKEN lives). Unset → no-op: the laptop runs everything as the login user.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

CLI_WRAPPER = "/usr/local/bin/coxd-claude"


def user() -> str | None:
    return os.environ.get("COXD_WORKER_USER") or None


def wrap(argv: list[str]) -> list[str]:
    """Prefix a command so it runs as the worker user with a clean identity:
    its own uid/gid and groups, no new privileges, no inherited capabilities."""
    u = user()
    if not u:
        return argv
    return ["setpriv", f"--reuid={u}", f"--regid={u}", "--init-groups",
            "--no-new-privs", "--inh-caps=-all",
            "env", f"HOME=/home/{u}", f"USER={u}", f"LOGNAME={u}", *argv]


def cli_path() -> str | None:
    """ClaudeAgentOptions.cli_path: the wrapper that execs `claude` via wrap()."""
    return CLI_WRAPPER if user() else None


def own(*paths: Path) -> None:
    """Hand a freshly created worktree (and its git admin dir) to the worker user."""
    u = user()
    if not u:
        return
    for p in paths:
        if p.exists():
            subprocess.run(["chown", "-R", f"{u}:{u}", str(p)], check=True,
                           capture_output=True)


def run_in_worktree(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess:
    """Read-only git (diff, show, rev-parse, worktree remove) inside a task worktree.

    The worker owns the worktree, including its `.git` pointer file and its admin dir,
    so it can point git at a config/hooks of its choosing. Root must therefore never
    run git there: these run as the worker, without credentials. Anything that needs
    the token (push, `gh pr create`) runs from the main repo checkout instead.
    """
    import envscrub  # local: envscrub has no deps, but keep sandbox import-light
    return subprocess.run(wrap(args), cwd=str(cwd), capture_output=True, text=True,
                          env=envscrub.scrubbed_env())

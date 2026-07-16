"""Deterministic gate — DESIGN-V35 (survives the pivot; policy, not transport).

Runs the registry's commands. A `full` task with no test/lint command is RED,
never a silent "skip" (the #99 "gate lied" defect). Zero tokens.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_gate(worktree: Path, entry: dict, path: str = "full") -> dict:
    steps: dict[str, str] = {}
    for name in ("test", "lint"):
        cmd = entry.get(name)
        if not cmd:
            if path == "full":
                return {"passed": False, "failing": name,
                        "reason": f"no {name} command in registry — RED, not skipped"}
            steps[name] = "skip"
            continue
        r = subprocess.run(["sh", "-c", cmd], cwd=worktree, capture_output=True, text=True)
        steps[name] = "ok" if r.returncode == 0 else "red"
        if r.returncode != 0:
            return {"passed": False, "failing": name, "steps": steps,
                    "reason": (r.stderr or r.stdout).strip()[-800:]}
    return {"passed": True, "steps": steps}


def diff(worktree: Path, base: str = "HEAD~1") -> str:
    r = subprocess.run(["git", "diff", f"{base}...HEAD"], cwd=worktree,
                       capture_output=True, text=True)
    if r.returncode != 0:  # e.g. only one commit — fall back to the last commit's diff
        r = subprocess.run(["git", "show", "HEAD"], cwd=worktree, capture_output=True, text=True)
    return r.stdout

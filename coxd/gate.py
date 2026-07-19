"""Deterministic gate — DESIGN-V35 (survives the pivot; policy, not transport).

Runs the registry's commands. A `full` task with no test/lint command is RED,
never a silent "skip" (the #99 "gate lied" defect). Zero tokens.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _check_migrations(worktree: Path) -> str | None:
    """Drizzle journal parity — the gate's e2e blind-spot fix.

    Every migration .sql must be registered in db/migrations/meta/_journal.json,
    or `drizzle-kit migrate` silently stops at the last journaled entry and the
    new columns never exist. Unit tests + `nest build` stay green (they don't hit
    a migrated DB) while CI e2e 500s on missing columns — the #101 defect, and
    #113 (0013) before it: 2 of 2 manual unsticks were this exact miss. Cheap,
    deterministic, zero tokens. Returns None when the repo has no drizzle journal.
    """
    journal = worktree / "db" / "migrations" / "meta" / "_journal.json"
    mig_dir = worktree / "db" / "migrations"
    if not journal.exists():
        return None  # not a drizzle repo — nothing to check
    try:
        entries = json.loads(journal.read_text(encoding="utf-8")).get("entries", [])
    except (ValueError, OSError) as e:
        return f"unreadable drizzle journal ({e})"
    tags = {e.get("tag") for e in entries}
    files = {p.stem for p in mig_dir.glob("*.sql")}
    unregistered = sorted(files - tags)
    if unregistered:
        return ("migration .sql not registered in drizzle journal — drizzle-kit "
                "migrate will skip it, leaving the columns missing: "
                + ", ".join(unregistered))
    orphaned = sorted(t for t in tags if t and t not in files)
    if orphaned:
        return ("drizzle journal references a missing .sql file: " + ", ".join(orphaned))
    return "ok"


def _base_ref(worktree: Path) -> str:
    """The branch point to diff against — for turbo's `--filter=[<ref>]` affected scope."""
    for ref in ("origin/main", "origin/master", "main", "master"):
        r = subprocess.run(["git", "rev-parse", "--verify", "-q", ref],
                           cwd=worktree, capture_output=True, text=True)
        if r.returncode == 0:
            return ref
    return "HEAD~1"


def run_gate(worktree: Path, entry: dict, path: str = "full") -> dict:
    # turbo monorepo → scope every step to the packages CHANGED vs the branch point,
    # so an unrelated package can't fail (or OOM) a task that never touched it.
    turbo = entry.get("runner") == "turbo"
    base = _base_ref(worktree) if turbo else None
    # Replicate the repo's CI-critical env so the gate matches CI, not a bare shell.
    # apps/web's jsdom suite OOMs without CI's NODE_OPTIONS heap bump — without this the
    # gate forces workers to hack package.json / drop tests to get green (the #101 defect).
    env = {**os.environ, **(entry.get("gate_env") or {})}
    steps: dict[str, str] = {}
    # migration-registration parity FIRST (fail fast, cheap): closes the gate's e2e
    # blind spot where unregistered drizzle migrations pass unit+build but 500 in CI.
    mig = _check_migrations(worktree)
    if mig is not None:
        steps["migrations"] = "ok" if mig == "ok" else "red"
        if mig != "ok":
            return {"passed": False, "failing": "migrations", "steps": steps, "reason": mig}
    # build catches what vitest/esbuild can't: `tsc` type errors that pass tests but
    # break `nest build` (the #112 lesson, re-proven on #100). Required-ness differs:
    # test/lint absent = UNKNOWN → RED; build absent = genuinely optional → skip.
    for name in ("test", "lint", "build"):
        cmd = entry.get(name)
        if cmd is False:  # deliberately no gate here (repo's CI doesn't run it) — NOT unknown
            steps[name] = "none"
            continue
        if not cmd:  # None/"" — RED for test/lint on a full task (don't lie); build is optional
            if path == "full" and name != "build":
                return {"passed": False, "failing": name,
                        "reason": f"no {name} command in registry — RED, not skipped"}
            steps[name] = "skip"
            continue
        if turbo:  # deterministic path to the local turbo bin; affected-only scope
            cmd = f"node_modules/.bin/turbo run {name} --filter='[{base}]'"
        r = subprocess.run(["sh", "-c", cmd], cwd=worktree, capture_output=True,
                           text=True, env=env)
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

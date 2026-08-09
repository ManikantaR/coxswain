"""Central per-repo command registry (DESIGN-V35 D23).

Retires `.cox/repo.yml` — nothing is committed to target repos. Each repo's
test/lint/build commands live in the coxswain home, auto-SCOUTED on first contact
by reading the repo's own manifests (package.json / pyproject / turbo.json), and
human-editable after. The gate reads commands from here; a `full` task with no
test/lint command goes RED, never a silent "skip" (that is exactly how MyMoney's
gate "passed" while running zero tests — the gate must not lie).
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("COXD_HOME", str(Path.home() / ".coxswain")))


def _path(repo_name: str) -> Path:
    return home() / "repos" / f"{repo_name}.json"


def load(repo_name: str) -> dict | None:
    p = _path(repo_name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def save(repo_name: str, entry: dict) -> None:
    p = _path(repo_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entry, indent=2), encoding="utf-8")


def scout(repo_path: Path) -> dict:
    """Discover commands from the repo's manifests. Deterministic, zero tokens.

    Values may be None (unknown) — the gate treats a None as RED for a full task.
    """
    entry: dict = {"test": None, "lint": None, "build": None,
                   "target_branch": "main", "source": None, "runner": None,
                   "gate_env": {},  # env the gate injects to match CI (e.g. NODE_OPTIONS heap)
                   # Opt-in auto-deploy after a merge lands (landed.py). Disabled by
                   # default — set enabled+command per repo. cwd defaults to the repo;
                   # gate_on_ci skips the deploy if the target branch's CI is red.
                   "deploy": {"enabled": False, "command": None, "cwd": None,
                              "gate_on_ci": True, "branch": "main"},
                   # Paths whose change forces a RED greenlight (read every line).
                   # Secrets/migrations by default; add the repo's sacred code per-repo.
                   "sacred_globs": [".env", "env*", "**/*secret*",
                                    "**/migrations/**", "**/alembic/**"]}
    pkg = repo_path / "package.json"
    pyproject = repo_path / "pyproject.toml"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {}) or {}
        except (ValueError, OSError):
            scripts = {}
        pm = "pnpm" if (repo_path / "pnpm-lock.yaml").exists() else \
             "yarn" if (repo_path / "yarn.lock").exists() else "npm run"
        for key in ("test", "lint", "build"):
            if key in scripts:
                entry[key] = f"{pm} {key}"
        # turbo monorepo: the gate scopes to CHANGED packages (a whole-repo run made
        # an unrelated apps/web OOM falsely fail a clean apps/api task — the #100 bug)
        if (repo_path / "turbo.json").exists():
            entry["runner"] = "turbo"
        entry["source"] = "package.json"
    elif pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        entry["test"] = "pytest -q"
        entry["lint"] = "ruff check ." if "ruff" in text else None
        entry["source"] = "pyproject.toml"
    # Deploy entrypoint: the repo owns its full deploy recipe in a single script;
    # coxd only TRIGGERS it, staying deploy-agnostic. Auto-detect the common ones so
    # onboarding is one confirm — but leave enabled=False (never auto-deploy a fresh
    # repo; the captain flips it on per repo).
    for candidate, cmd in (("deploy-to-nas.sh", "./deploy-to-nas.sh"),
                           ("deploy.sh", "./deploy.sh"),
                           ("bin/deploy", "bin/deploy"),
                           ("fly.toml", "fly deploy")):
        if (repo_path / candidate).exists():
            entry["deploy"]["command"] = cmd
            break
    return entry


def get_or_scout(repo_name: str, repo_path: Path) -> dict:
    """The registry entry, scouting + caching it on first contact with a repo."""
    entry = load(repo_name)
    if entry is None:
        entry = scout(repo_path)
        save(repo_name, entry)
    return entry

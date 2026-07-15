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
                   "target_branch": "main", "source": None}
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
        entry["source"] = "package.json"
    elif pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        entry["test"] = "pytest -q"
        entry["lint"] = "ruff check ." if "ruff" in text else None
        entry["source"] = "pyproject.toml"
    return entry


def get_or_scout(repo_name: str, repo_path: Path) -> dict:
    """The registry entry, scouting + caching it on first contact with a repo."""
    entry = load(repo_name)
    if entry is None:
        entry = scout(repo_path)
        save(repo_name, entry)
    return entry

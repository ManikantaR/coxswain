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
import math
import os
import threading
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


# Earned auto-merge (DECISIONS D29). `manual` (default) → `proposed` (streak hit the
# threshold, owner notified) → `auto` (ONLY via approve_auto_merge, the board button).
MERGE_LEVELS = ("manual", "proposed", "auto")
AUTO_MERGE_STREAK = 10
_level_lock = threading.Lock()


def merge_level(repo_name: str) -> str:
    lvl = (load(repo_name) or {}).get("merge_level")
    return lvl if lvl in MERGE_LEVELS else "manual"


def propose_auto_merge(repo_name: str) -> bool:
    """manual → proposed. True only on the transition (so the owner is pinged once).
    Never touches `proposed`/`auto`, and never sets `auto`."""
    with _level_lock:
        entry = load(repo_name)
        if entry is None or merge_level(repo_name) != "manual":
            return False
        entry["merge_level"] = "proposed"
        save(repo_name, entry)
        return True


def withdraw_proposal(repo_name: str) -> bool:
    """proposed → manual (the streak broke before the owner approved). Never touches `auto`."""
    with _level_lock:
        entry = load(repo_name)
        if entry is None or merge_level(repo_name) != "proposed":
            return False
        entry["merge_level"] = "manual"
        save(repo_name, entry)
        return True


def approve_auto_merge(repo_name: str) -> bool:
    """proposed → auto. The ONLY function that sets `auto` (called by the board's
    'Approve auto-merge' action). False if the repo has no pending proposal."""
    with _level_lock:
        entry = load(repo_name)
        if entry is None or merge_level(repo_name) != "proposed":
            return False
        entry["merge_level"] = "auto"
        save(repo_name, entry)
        return True


def daily_cost_cap(repo_name: str) -> float | None:
    """Optional per-repo USD/day cap from the registry; None (default) = no cap."""
    entry = load(repo_name) or {}
    cap = entry.get("daily_cost_cap")
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
        return None
    return float(cap)


DEFAULT_TASK_BUDGET_USD = 3.0


def task_budget(repo_name: str) -> float:
    """Optional per-repo per-task USD budget threshold (`budget_usd`); defaults to
    DEFAULT_TASK_BUDGET_USD. Display-only (drives the board's over-budget chip)."""
    entry = load(repo_name) or {}
    raw = entry.get("budget_usd")
    if (isinstance(raw, bool) or not isinstance(raw, (int, float))
            or not math.isfinite(raw) or raw <= 0):
        return DEFAULT_TASK_BUDGET_USD
    return float(raw)


def autofix_cmds(entry: dict | None) -> list[str]:
    """Pre-gate fixer commands (formatters/linters in fix mode; see autofix.py). Opt-in:
    a registry entry without a list of strings under `autofix` runs nothing."""
    raw = (entry or {}).get("autofix")
    if not isinstance(raw, list):
        return []
    return [c.strip() for c in raw if isinstance(c, str) and c.strip()]


def ci_parity_ignore(entry: dict | None) -> list[str]:
    """CI steps (normalized commands) whose absence from the gate is deliberate."""
    raw = (entry or {}).get("ci_parity_ignore")
    if not isinstance(raw, list):
        return []
    return [c.strip() for c in raw if isinstance(c, str) and c.strip()]


FIX_MODES = ("resume", "fresh")


def fix_mode(repo_or_entry: str | dict | None) -> str:
    """How review-/gate-triggered fix rounds run: `resume` (default: the original worker
    session, full history re-sent each turn) or `fresh` (a new session with a compact
    brief). Anything but exactly "fresh" is `resume`."""
    entry = repo_or_entry if isinstance(repo_or_entry, dict) else (
        load(repo_or_entry) if isinstance(repo_or_entry, str) else None)
    mode = (entry or {}).get("fix_mode")
    return "fresh" if isinstance(mode, str) and mode == "fresh" else "resume"


DATA_GUARD_NOTE = ("\n\n---\nDATA GUARD: this is a document repo. Use synthetic fixtures only; "
                   "never open data folders or real documents. New files matching {globs} "
                   "may only be added under `{fixture}`.")


def data_guard_brief(entry: dict | None, brief: str) -> str:
    """Append the synthetic-fixtures-only rule when the repo sets `data_globs`."""
    globs = [g for g in ((entry or {}).get("data_globs") or []) if isinstance(g, str)]
    if not globs:
        return brief
    fixture = (entry or {}).get("fixture_dir") or "(no fixture dir configured)"
    return brief + DATA_GUARD_NOTE.format(globs=", ".join(f"`{g}`" for g in globs),
                                          fixture=fixture)


def _ci_text(repo_path: Path) -> str:
    """All workflow YAML text (lower-cased); "" when none. Used to scout opt-in formatters
    only where CI already enforces them — never reformat a repo whose CI doesn't care."""
    parts = []
    for f in sorted((repo_path / ".github" / "workflows").glob("*.y*ml")):
        try:
            parts.append(f.read_text(encoding="utf-8"))
        except OSError:
            pass
    return "\n".join(parts).lower()


def scout(repo_path: Path) -> dict:
    """Discover commands from the repo's manifests. Deterministic, zero tokens.

    Values may be None (unknown) — the gate treats a None as RED for a full task.
    """
    entry: dict = {"test": None, "lint": None, "build": None,
                   "target_branch": "main", "source": None, "runner": None,
                   "merge_level": "manual",  # manual | proposed | auto (earned; see D29)
                   "autofix": [],  # pre-gate fixers (autofix.py), e.g. ["ruff format ."]
                   "ci_parity_ignore": [],  # CI steps the gate deliberately skips
                   "intake": False,  # opt-in: poll for `coxd-ready` issues (intake.py)
                   "gate_env": {},  # env the gate injects to match CI (e.g. NODE_OPTIONS heap)
                   # globs left out of the review diff; None = gate.DEFAULT_REVIEW_EXCLUDE,
                   # a list replaces it
                   "review_exclude": None,
                   # Opt-in auto-deploy after a merge lands (landed.py). Disabled by
                   # default — set enabled+command per repo. cwd defaults to the repo;
                   # gate_on_ci skips the deploy if the target branch's CI is red.
                   "deploy": {"enabled": False, "command": None, "cwd": None,
                              "gate_on_ci": True, "branch": "main",
                              # Dir names; a merge touching any path under one is never
                              # deployed unattended (#53).
                              "migration_dirs": ["migrations", "alembic"]},
                   # Paths whose change forces a RED greenlight (read every line).
                   # Secrets/migrations by default; add the repo's sacred code per-repo.
                   # Document repos: new files matching data_globs outside fixture_dir
                   # block the PII gate (empty = off). e.g. ["**/*.pdf", "inbox/**"].
                   "data_globs": [], "fixture_dir": None,
                   "sacred_globs": [".env", "env*", "**/*secret*",
                                    "**/migrations/**", "**/alembic/**"]}
    pkg = repo_path / "package.json"
    pyproject = repo_path / "pyproject.toml"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {}) or {}
            if not isinstance(scripts, dict):
                scripts = {}
        except (ValueError, OSError, AttributeError):
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
        pjson = {}
        try:
            pjson = json.loads(pkg.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
        if not isinstance(pjson, dict):
            pjson = {}
        deps = {k: v for key in ("devDependencies", "dependencies")
                if isinstance(pjson.get(key), dict) for k, v in pjson[key].items()}
        run = "pnpm exec" if pm == "pnpm" else "yarn" if pm == "yarn" else "npx --no-install"
        if "eslint" in deps and any((repo_path / f).exists() for f in (
                "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
                ".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.cjs")):
            entry["autofix"].append(f"{run} eslint --fix .")
        if "prettier" in deps and ("prettier" in _ci_text(repo_path)
                                   or any("format" in k for k in scripts)):
            entry["autofix"].append(f"{run} prettier --write .")
    elif pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        entry["test"] = "pytest -q"
        entry["lint"] = "ruff check ." if "ruff" in text else None
        if "ruff" in text:
            entry["autofix"] = ["ruff check --fix ."]
            if "ruff format" in _ci_text(repo_path):
                entry["autofix"].append("ruff format .")
        entry["source"] = "pyproject.toml"
    elif (repo_path / "configuration.yaml").exists():
        # Home Assistant config repo: yamllint is the only gate; never deployable.
        entry["test"] = entry["lint"] = "yamllint ."  # a full task needs a test cmd or goes RED
        entry["source"] = "configuration.yaml"
        entry["sacred_globs"] += ["secrets.yaml", ".storage/**"]
        entry["deploy"]["command"] = None
        return entry
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

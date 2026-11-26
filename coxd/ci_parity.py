"""Gate/CI parity: warn when a repo's gate commands differ from what its CI runs.

A green gate followed by red GitHub CI costs a whole fix round. Parity never edits a
workflow or the gate — it only reports (board chip + per-repo note):
  mismatched  the gate runs the same tool as a CI step but with different arguments
  ci_only     CI steps no gate command covers (e.g. `mypy`, an e2e job)
A deliberate gap is silenced per repo with the registry's `ci_parity_ignore` list
(normalized command or just its tool name).
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import registry

try:  # runtime-optional: coxd's own deps don't list pyyaml; fall back to a line scan
    import yaml as _yaml
except ImportError:  # pragma: no cover - exercised by test via _scan_runs
    _yaml = None

_PREFIXES = ("python3 -m ", "python -m ", "npm run ", "pnpm run ", "pnpm exec ", "pnpm ",
             "yarn run ", "yarn ", "npx ", "uv run ", "poetry run ", "npm ")
# Setup / plumbing, not checks: dropped from the CI side.
_SETUP_FIRST = {
    "pip", "pip3", "git", "echo", "cd", "mkdir", "export", "apt", "apt-get", "sudo", "curl",
    "wget", "cat", "ls", "set", "source", "corepack", "true", "install", "ci", "i", "sync",
    "add", "setup", "env", "printf", "python", "python3", "node", "uv", "poetry", "rm", "cp",
    "mv"}


def _flatten(text: str) -> list[str]:
    """One command per entry: join `\\` continuations, split on newline, `&&`, `;`."""
    text = re.sub(r"\\\r?\n\s*", " ", text)
    out: list[str] = []
    for line in text.splitlines():
        for part in re.split(r"&&|;", line):
            part = part.strip()
            if part and not part.startswith("#"):
                out.append(part)
    return out


def normalize(cmd: str) -> str | None:
    """Canonical form of one command ('pnpm test' -> 'test', 'python -m pytest -q' ->
    'pytest -q'); None for setup/plumbing."""
    c = " ".join(cmd.split())
    c = re.sub(r"^(\w+=\S+\s+)+", "", c)  # leading VAR=val
    for p in _PREFIXES:
        if c.startswith(p):
            c = c[len(p):]
            break
    first = c.split(" ", 1)[0] if c else ""
    if re.match(r"^\S+ (install|ci)\b", c):  # e.g. `playwright install --with-deps`
        return None
    if not first or first in _SETUP_FIRST or first.startswith(("$", "-", "[", "if", "fi")):
        return None
    return c


def _scan_runs(text: str) -> list[str]:
    """Dependency-free fallback: the `run:` values of a workflow (inline or block scalar)."""
    out: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)(?:-\s+)?run:\s*(.*)$", lines[i])
        i += 1
        if not m:
            continue
        indent, rest = len(m.group(1)), m.group(2).strip()
        if rest and rest[0] in "|>":
            block: list[str] = []
            while i < len(lines) and (not lines[i].strip()
                                      or len(lines[i]) - len(lines[i].lstrip()) > indent):
                block.append(lines[i].strip())
                i += 1
            out.append("\n".join(block))
        elif rest:
            out.append(rest.strip("'\""))
    return out


def _yaml_runs(doc) -> list[str]:
    out: list[str] = []
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    for job in (jobs.values() if isinstance(jobs, dict) else []):
        for step in (job.get("steps") if isinstance(job, dict) else None) or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                out.append(step["run"])
    return out


def ci_commands(repo_path: Path) -> list[str]:
    """Normalized, de-duplicated check commands from .github/workflows/*.y(a)ml."""
    seen: dict[str, None] = {}
    for f in sorted((Path(repo_path) / ".github" / "workflows").glob("*.y*ml")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        runs: list[str] | None = None
        if _yaml is not None:
            try:
                runs = _yaml_runs(_yaml.safe_load(text))
            except Exception:
                runs = None
        if runs is None:
            runs = _scan_runs(text)
        for run in runs:
            for part in _flatten(run):
                n = normalize(part)
                if n:
                    seen[n] = None
    return list(seen)


def _tool(cmd: str) -> str:
    return cmd.split(" ", 1)[0]


_SUBCMD_TOOLS = {"ruff", "cargo", "go", "docker"}


def _op(cmd: str) -> str:
    """Tool plus, for multi-command tools, its subcommand (`ruff check` != `ruff format`)."""
    parts = cmd.split()
    if parts and parts[0] in _SUBCMD_TOOLS and len(parts) > 1 and not parts[1].startswith("-"):
        return " ".join(parts[:2])
    return _tool(cmd)


def check(repo_name: str, repo_path: Path, entry: dict | None) -> dict:
    """{ok, mismatched:[{gate,ci}], ci_only:[...], note} for one repo."""
    entry = entry or {}
    ci = ci_commands(repo_path)
    gate_cmds: list[str] = []
    for key in ("test", "lint", "build"):
        v = entry.get(key)
        if isinstance(v, str):
            for part in _flatten(v):
                n = normalize(part)
                if n:
                    gate_cmds.append(n)
    ignore = set(registry.ci_parity_ignore(entry))
    mismatched: list[dict] = []
    ci_only: list[str] = []
    gate_set = set(gate_cmds)
    for c in ci:
        if c in gate_set or c in ignore or _tool(c) in ignore:
            continue
        same_tool = [g for g in gate_cmds if _op(g) == _op(c)]
        if same_tool:
            mismatched.append({"gate": same_tool[0], "ci": c})
        else:
            ci_only.append(c)
    ok = not mismatched and not ci_only
    if not ci:
        note = f"{repo_name}: no CI check steps found"
    elif ok:
        note = f"{repo_name}: gate matches CI"
    else:
        bits = []
        if mismatched:
            bits.append("differs from CI: " + "; ".join(
                f"gate `{m['gate']}` vs CI `{m['ci']}`" for m in mismatched))
        if ci_only:
            bits.append("CI steps the gate cannot run: " + ", ".join(f"`{c}`" for c in ci_only))
        note = f"{repo_name}: " + " — ".join(bits)
    return {"ok": ok, "mismatched": mismatched, "ci_only": ci_only, "note": note}


# --- board cache: parse workflows once per change, never per poll -----------------
_cache: dict[str, tuple[tuple, dict]] = {}
_lock = threading.Lock()


def _fingerprint(repo_path: Path, entry: dict) -> tuple:
    wf = sorted((Path(repo_path) / ".github" / "workflows").glob("*.y*ml"))
    stamps = []
    for f in wf:
        try:
            stamps.append((f.name, f.stat().st_mtime_ns))
        except OSError:
            pass
    return (tuple(stamps), entry.get("test"), entry.get("lint"), entry.get("build"),
            tuple(registry.ci_parity_ignore(entry)))


def cached(repo_name: str, repo_path: str | Path | None, entry: dict | None) -> dict | None:
    """check() memoised on workflow mtimes + gate commands; None if the repo is unknown."""
    if not repo_path or not Path(repo_path).is_dir():
        return None
    entry = entry or {}
    fp = _fingerprint(Path(repo_path), entry)
    with _lock:
        hit = _cache.get(repo_name)
        if hit and hit[0] == fp:
            return hit[1]
    res = check(repo_name, Path(repo_path), entry)
    with _lock:
        _cache[repo_name] = (fp, res)
    return res

"""Repo registry + defanged clone-on-demand (DESIGN-VNEXT D17).

A curated clone-root holds the repos coxswain works on. The dispatch picker
lists what is already there; a git URL is cloned in on demand, deduped by slug,
and always operated through a worktree (worktree.py). Freshly cloned repos are
UNTRUSTED until the captain confirms — dispatch refuses an untrusted repo, so
nothing runs its test/lint (the gate) until a human okays it. That, plus a
non-recursive clone with hooks disabled, neutralises clone-time hook RCE
(CVE-2025-48384). Pre-existing repos the captain put in the root themselves are
trusted implicitly.

Own repos at home and work (GitHub + Azure DevOps git-backed, so a single
`git clone` path ports to both); the worker never gets push creds (DESIGN P6) —
a captain-held credential does the clone.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import home, proc


class CloneError(RuntimeError):
    """A clone/resolve could not produce a usable git repo."""


def repo_root() -> Path:
    """Curated clone-root: env COX_REPO_ROOT, else ~/repo."""
    raw = os.environ.get("COX_REPO_ROOT")
    return Path(raw).expanduser() if raw else Path.home() / "repo"


_URL_RE = re.compile(r"^(https?://|git@|ssh://|git://)")


def is_git_url(ref: str) -> bool:
    """True if *ref* looks like a git URL (vs. a local name/path)."""
    return bool(_URL_RE.match(ref.strip()))


def slug_from_url(url: str) -> str:
    """Directory name for a clone: last path/scp segment, minus a .git suffix."""
    tail = re.split(r"[/:]", url.strip().rstrip("/"))[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return tail or "repo"


# --- trust state ------------------------------------------------------------
# We store the set of UNTRUSTED (freshly cloned, unconfirmed) repos, not trusted
# ones: anything the captain already had in the root is trusted by default, and
# only a coxswain-cloned repo starts life gated.


def _pending_file() -> Path:
    return home.state_dir() / "untrusted_repos.json"


def _key(path: Path) -> str:
    return str(path.expanduser().resolve())


def _load_pending() -> set[str]:
    p = _pending_file()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (ValueError, OSError):
        return set()


def _save_pending(paths: set[str]) -> None:
    home.ensure_home()
    _pending_file().write_text(json.dumps(sorted(paths), indent=2), encoding="utf-8")


def is_trusted(path: Path) -> bool:
    return _key(path) not in _load_pending()


def mark_trusted(path: Path) -> None:
    """Captain confirms a freshly cloned repo — dispatch may now use it."""
    pending = _load_pending()
    pending.discard(_key(path))
    _save_pending(pending)


def _mark_untrusted(path: Path) -> None:
    pending = _load_pending()
    pending.add(_key(path))
    _save_pending(pending)


# --- listing + resolution ---------------------------------------------------


@dataclass(frozen=True)
class RepoInfo:
    name: str
    path: Path
    trusted: bool


@dataclass(frozen=True)
class RepoResolution:
    path: Path
    cloned: bool  # True only when this call performed a fresh clone
    trusted: bool
    url: str | None


def list_local_repos() -> list[RepoInfo]:
    """Git repos directly under the clone-root, for the dispatch picker."""
    root = repo_root()
    if not root.exists():
        return []
    out: list[RepoInfo] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / ".git").exists():
            out.append(RepoInfo(name=child.name, path=child.resolve(), trusted=is_trusted(child)))
    return out


def _resolve_local(ref: str) -> Path:
    """A bare name resolves under the clone-root; a path is taken as-is."""
    if ref.startswith("~") or ref.startswith("/") or "/" in ref:
        return Path(ref).expanduser().resolve()
    return (repo_root() / ref).resolve()


def clone(url: str) -> RepoResolution:
    """Clone *url* into the clone-root (deduped by slug), defanged.

    Non-recursive (no submodule hook execution) with hooks disabled; the fresh
    clone is marked untrusted until the captain confirms.
    """
    root = repo_root()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / slug_from_url(url)
    if (dest / ".git").exists():
        return RepoResolution(path=dest.resolve(), cloned=False, trusted=is_trusted(dest), url=url)
    if dest.exists():
        raise CloneError(f"{dest} exists but is not a git repo")
    proc.run(
        ["git", "clone", "--no-recurse-submodules", "-c", "core.hooksPath=/dev/null",
         url, str(dest)],
    )
    _mark_untrusted(dest)
    return RepoResolution(path=dest.resolve(), cloned=True, trusted=False, url=url)


def resolve(ref: str) -> RepoResolution:
    """Resolve a picker entry — a git URL (clone-on-demand) or a local name/path."""
    ref = ref.strip()
    if not ref:
        raise CloneError("repo reference is empty")
    if is_git_url(ref):
        return clone(ref)
    path = _resolve_local(ref)
    return RepoResolution(path=path, cloned=False, trusted=is_trusted(path), url=None)

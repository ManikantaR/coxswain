"""Per-repo config from <repo>/.cox/repo.yml (DESIGN §2.4, config/repo.yml.example).

Configured test/lint commands are the strong, tokenless gate path. Missing
commands are allowed (gate marks them 'skip' loudly) but discouraged.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoConfig:
    test_cmd: str | None
    lint_cmd: str | None
    review: str  # "full" | "none"
    target_branch: str
    scm: str  # github | azdevops | tfs | local
    boundaries: tuple[str, ...] = ()  # 🚫 never-touch path globs (P3, gate-enforced)
    max_files: int | None = None  # gate-red if the diff touches more files than this


_DEFAULT = RepoConfig(
    test_cmd=None, lint_cmd=None, review="full", target_branch="main", scm="github"
)


def load_repo_config(repo_or_worktree: Path) -> RepoConfig:
    cfg_path = repo_or_worktree / ".cox" / "repo.yml"
    if not cfg_path.exists():
        return _DEFAULT
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        # A present repo.yml we can't parse must not silently use defaults.
        from .models import BosunConfigError

        raise BosunConfigError(f"{cfg_path} exists but PyYAML is missing") from None
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    commands = data.get("commands", {}) or {}
    bounds = data.get("boundaries") or []
    max_files = data.get("max_files")
    return RepoConfig(
        test_cmd=commands.get("test"),
        lint_cmd=commands.get("lint"),
        review=str(data.get("review", "full")),
        target_branch=str(data.get("target_branch", "main")),
        scm=str(data.get("scm", "github")),
        boundaries=tuple(str(b) for b in bounds if str(b).strip()),
        max_files=int(max_files) if isinstance(max_files, int) else None,
    )

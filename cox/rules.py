"""Compounding repo-rules (P1, docs/RESEARCH-loop-engineering.md).

Cherny's "write the rule, not the correction": every review finding or captain
correction can become a durable one-line rule so the implementer stops repeating
it. Rules live in coxswain's HOME (never the per-task worktree, which is thrown
away), keyed by repo, and are injected into every future implementer brief for
that repo. Append-only and captain-curated — mirrors the "typed needs-human, no
dumping ground" discipline, so this file stays a short list of real lessons.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from . import home


def rules_path(repo: str) -> Path:
    safe = (repo or "unknown").replace("/", "-")
    return home.home() / "rules" / f"{safe}.md"


def add_rule(repo: str, text: str) -> bool:
    """Append a one-line rule for *repo*. Returns False for empty/duplicate text."""
    text = " ".join((text or "").split())
    if not text:
        return False
    existing = {_rule_text(ln) for ln in list_rules(repo)}
    if text in existing:
        return False
    p = rules_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- {text}  ({datetime.date.today().isoformat()})\n")
    return True


def list_rules(repo: str) -> list[str]:
    p = rules_path(repo)
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _rule_text(line: str) -> str:
    """Strip the leading '- ' and trailing '(date)' for dedupe/display."""
    s = line.strip().lstrip("-").strip()
    if s.endswith(")") and "(" in s:
        s = s[: s.rfind("(")].strip()
    return s


def rules_block(repo: str) -> str:
    """The brief section injected into every implementer prompt for *repo*."""
    lines = list_rules(repo)
    if not lines:
        return ""
    return "## Learned rules for this repo — do NOT repeat these past mistakes\n" + "\n".join(lines)

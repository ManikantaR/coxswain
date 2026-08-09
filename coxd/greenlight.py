"""Auto-greenlight — tier a pr_ready PR so review is one-tap on low-risk changes.

coxd already computes every input: a pr_ready task's gate is green by construction
(gate-red goes needs_human), the Opus review left a verdict + advisory findings, and
the diff stat is a git numstat. Combine those with a per-repo `sacred_globs` list into
GREEN / AMBER / RED and render a "look here first" header into the PR body. GREEN =
pre-labeled safe-to-merge (one click); the merge itself is still the captain's word
(the Prime Directive holds). No new signal, no LLM call — pure rendering.
"""
from __future__ import annotations

import fnmatch

# Above these a change is no longer "small/mechanical" → at least AMBER.
_GREEN_MAX_FILES = 5
_GREEN_MAX_LINES = 120

_ICON = {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴"}
_LABEL = {"GREEN": "safe · one-click merge", "AMBER": "skim before merge",
          "RED": "read every line"}
_SEV_RANK = {"critical": 0, "high": 0, "med": 1, "medium": 1, "low": 2}


def _sev(f: dict) -> str:
    return str(f.get("severity", "")).lower()


def tier(findings: list[dict], verdict: str | None, changed_files: list[str],
         added: int, deleted: int, sacred_globs: list[str] | None) -> tuple[str, list[str]]:
    """Classify a pr_ready PR. Returns (GREEN|AMBER|RED, reasons)."""
    reasons: list[str] = []
    sacred_hit = sorted({f for f in changed_files
                         if any(fnmatch.fnmatch(f, g) for g in (sacred_globs or []))})
    highs = [f for f in findings if _sev(f) in ("high", "critical")]
    meds = [f for f in findings if _sev(f) in ("med", "medium")]
    big = len(changed_files) > _GREEN_MAX_FILES or (added + deleted) > _GREEN_MAX_LINES
    rejected = bool(verdict) and str(verdict).lower() == "reject"

    if sacred_hit or highs or rejected:
        if sacred_hit:
            reasons.append(f"touches sacred path(s): {', '.join(sacred_hit[:3])}")
        if highs:
            reasons.append(f"{len(highs)} high-severity finding(s)")
        if rejected:
            reasons.append("review verdict = reject")
        return "RED", reasons
    if meds or big:
        if meds:
            reasons.append(f"{len(meds)} medium finding(s)")
        if big:
            reasons.append(f"large diff ({len(changed_files)} files, +{added}/-{deleted})")
        return "AMBER", reasons
    reasons.append(f"clean: gate green, no high/med findings, small diff "
                   f"({len(changed_files)} files, +{added}/-{deleted})")
    return "GREEN", reasons


def header(tier_: str, reasons: list[str], findings: list[dict]) -> str:
    """A compact, risk-ranked 'look here first' block for the PR body."""
    out = [f"### {_ICON.get(tier_, '⚪')} coxd greenlight — {tier_} · {_LABEL[tier_]}"]
    if reasons:
        out.append("Why: " + "; ".join(reasons))
    ranked = sorted(findings, key=lambda f: _SEV_RANK.get(_sev(f), 3))
    if ranked:
        out.append("\n**Look here first:**")
        for f in ranked[:6]:
            loc = f"{f.get('file', '')}:{f.get('line', '')}".strip(":")
            out.append(f"- [{f.get('severity', '?')}]{' ' + loc + ' —' if loc else ''} "
                       f"{f.get('summary', '')}")
    return "\n".join(out)

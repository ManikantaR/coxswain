"""Evidence contract check (T-09, salvage: relay relay_control.py _collect_evidence).

Contract first: the brief names exact filenames (evidence/test-output.txt or
evidence/SKIP.md). If those exist, non-empty, and newer than dispatch, the
contract passed. Only if that fails do we fall back to relay's fuzzy sweep —
and we log a contract-violation warning, because a sweep firing often means the
brief is wrong, not that the miss should be normalized (DESIGN §2.6).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import store


@dataclass(frozen=True)
class EvidenceReport:
    ok: bool
    files: list[str]
    contract_violation: bool
    note: str


def check(task_id: str, dispatched_at: float) -> EvidenceReport:
    ev = store.task_data_dir(task_id) / "evidence"
    required = ["test-output.txt", "SKIP.md"]

    def _valid(p: Path) -> bool:
        return p.exists() and p.stat().st_size > 0 and p.stat().st_mtime >= dispatched_at

    hits = [name for name in required if _valid(ev / name)]
    if hits:
        return EvidenceReport(ok=True, files=hits, contract_violation=False, note="contract met")

    # Fallback: fuzzy sweep for anything evidence-shaped the worker misplaced.
    swept = _sweep(ev)
    if swept:
        return EvidenceReport(
            ok=True,
            files=swept,
            contract_violation=True,
            note="CONTRACT VIOLATION: evidence found by fuzzy sweep, not at the "
            "expected path — fix the brief template if this recurs",
        )
    return EvidenceReport(
        ok=False, files=[], contract_violation=False, note="no evidence produced"
    )


def _sweep(ev: Path) -> list[str]:
    if not ev.exists():
        return []
    out = []
    for p in ev.rglob("*"):
        if p.is_file() and p.stat().st_size > 0:
            out.append(str(p.relative_to(ev)))
    return out

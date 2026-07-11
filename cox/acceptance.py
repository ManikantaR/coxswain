"""Per-task acceptance criteria + implementer self-check (P2/D2).

Cherny's `/goal` + Karpathy's "close the loop twice": a task carries a typed
definition-of-done. The implementer must self-verify against it and write the
results BEFORE the deterministic gate runs — so the loop closes once by the
agent's own assertion and again by the gate. Criteria come from the plan phase's
'Acceptance criteria' / 'How to verify' section, or are typed at dispatch.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import store


def acceptance_path(task_id: str) -> Path:
    return store.task_data_dir(task_id) / "acceptance.json"


def selfcheck_path(task_id: str) -> Path:
    return store.task_data_dir(task_id) / "evidence" / "selfcheck.json"


def save_criteria(task_id: str, items: list[str]) -> list[str]:
    cleaned = [" ".join(i.split()) for i in items if i and i.strip()]
    if not cleaned:
        return []
    p = acceptance_path(task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")
    return cleaned


def load_criteria(task_id: str) -> list[str]:
    p = acceptance_path(task_id)
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return [str(x) for x in d] if isinstance(d, list) else []


_ITEM_RE = re.compile(r"^\s*[-*]\s*(?:\[[ xX]?\]\s*)?(.+?)\s*$")


def parse_from_plan(plan_text: str) -> list[str]:
    """Bullet/checkbox items under an 'Acceptance criteria' or 'How to verify' heading."""
    items: list[str] = []
    grabbing = False
    for line in plan_text.splitlines():
        if line.lstrip().startswith("#"):
            h = line.strip().lower()
            grabbing = "acceptance criteria" in h or "how to verify" in h
            continue
        if grabbing:
            m = _ITEM_RE.match(line)
            if m and m.group(1).strip():
                items.append(m.group(1).strip())
            elif line.strip() and not line.startswith(" "):
                grabbing = False  # a non-list paragraph ends the section
    return items


def load_selfcheck(task_id: str) -> list[dict]:
    """The implementer's [{item, ok, note}] assertions, or []."""
    p = selfcheck_path(task_id)
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []


def criteria_block(task_id: str) -> str:
    """The acceptance section injected into the implementer brief (empty if none)."""
    items = load_criteria(task_id)
    if not items:
        return ""
    checklist = "\n".join(f"- [ ] {c}" for c in items)
    return (
        "## Acceptance criteria — satisfy every item, then self-verify\n" + checklist + "\n\n"
        "Before you report done, write `evidence/selfcheck.json`: a JSON array of "
        '{"item": "<criterion>", "ok": true|false, "note": "how you verified"} for every '
        "criterion above. Do NOT report done unless every `ok` is true."
    )


def status(task_id: str) -> list[dict]:
    """Merge criteria with the self-check for the card checklist (D2).

    Each: {item, self} where self is 'pass' | 'fail' | 'unchecked'.
    """
    checks = {" ".join(str(c.get("item", "")).split()): c for c in load_selfcheck(task_id)}
    out = []
    for item in load_criteria(task_id):
        c = checks.get(" ".join(item.split()))
        self_ = "unchecked" if c is None else ("pass" if c.get("ok") else "fail")
        out.append({"item": item, "self": self_, "note": (c or {}).get("note", "")})
    return out

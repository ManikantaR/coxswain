"""Deterministic PII / secret gate (no model, no network). Hits carry the rule name
and location ONLY — never the matched text — so events/logs can't leak it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import registry


@dataclass(frozen=True)
class Hit:
    rule: str
    location: str


_SECRET_RES = [re.compile(p) for p in (
    r"gh[pousr]_[A-Za-z0-9]{36,}",
    r"github_pat_[A-Za-z0-9_]{22,}",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----",
    r"sk-ant-[A-Za-z0-9_-]{20,}",
)]
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_CARD_RE = re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-]?\d)(?!\w)")

# Well-known published test card numbers (processor docs).
_TEST_CARDS = {
    "4111111111111111", "4242424242424242", "4012888888881881", "4000056655665556",
    "5555555555554444", "5105105105105100", "5200828282828210", "2223003122003222",
    "378282246310005", "371449635398431", "378734493671000",
    "6011111111111117", "6011000990139424", "30569309025904", "38520000023237",
    "3530111333300000", "3566002020360505",
}


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _has_card(text: str) -> bool:
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and digits not in _TEST_CARDS and _luhn(digits):
            return True
    return False


def denylist_path() -> Path:
    return registry.home() / "pii-denylist.txt"


def load_denylist() -> tuple[list[str], bool]:
    """(terms, missing). Read fresh each call; a missing file is not an error."""
    p = denylist_path()
    if not p.is_file():
        return [], True
    terms = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            terms.append(ln)
    return terms, False


def scan(items: dict[str, str], denylist: list[str]) -> list[Hit]:
    deny = [re.compile(r"(?<!\w)" + re.escape(t) + r"(?!\w)", re.IGNORECASE)
            for t in denylist if t and t.strip()]
    hits: list[Hit] = []
    for loc, text in items.items():
        if any(r.search(text) for r in _SECRET_RES):
            hits.append(Hit("secret", loc))
        if _has_card(text):
            hits.append(Hit("card", loc))
        if _SSN_RE.search(text):
            hits.append(Hit("ssn", loc))
        if any(r.search(text) for r in deny):
            hits.append(Hit("denylist", loc))
    return hits

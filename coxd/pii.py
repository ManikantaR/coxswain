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
_CARD_RUN_RE = re.compile(r"(?<![\w-])\d(?:[ -]?\d)*(?![\w-]?\d)(?!\w)")

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
    """Each digit run is split into separator-delimited groups; every contiguous span of
    whole groups totalling 13-19 digits is Luhn-checked, so a card next to a short group
    (e.g. an expiry) is still caught while one unbroken run is checked only as a whole.
    Spans overlapping a published test card number are skipped."""
    for m in _CARD_RUN_RE.finditer(text):
        groups = re.split(r"[ -]", m.group())
        digits = "".join(groups)
        covered = [False] * len(digits)
        for tc in _TEST_CARDS:
            i = digits.find(tc)
            while i != -1:
                for j in range(i, i + len(tc)):
                    covered[j] = True
                i = digits.find(tc, i + 1)
        starts = [0]
        for g in groups:
            starts.append(starts[-1] + len(g))
        for si in range(len(groups)):
            for ei in range(si + 1, len(groups) + 1):
                lo, hi = starts[si], starts[ei]
                if hi - lo > 19:
                    break  # bounds the inner loop (a long run of groups must not go O(G^2))
                if (hi - lo >= 13 and not any(covered[lo:hi]) and set(digits[lo:hi]) != {"0"}
                        and _luhn(digits[lo:hi])):
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


def allow_path() -> Path:
    return registry.home() / "pii-allow.txt"


def load_allow() -> set[tuple[str, str, str]]:
    """Owner-maintained false-positive overrides: `<rule>|<location>|<task-id>` per line.
    Lives in COXD_HOME. Under the sandbox (COXD_WORKER_USER) that is root-owned and workers
    get a scratch COXD_HOME; in unsandboxed laptop mode a worker shares the uid and CAN
    write it, so only trust it with the sandbox on. `path:<i>` locations are index-based
    and shift when the changed-path set changes — prefer `diff:`/`commit:` entries.
    Read fresh each scan. `secret` entries are always ignored."""
    p = allow_path()
    out: set[tuple[str, str, str]] = set()
    if not p.is_file():
        return out
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = [x.strip() for x in ln.split("|")]
        if len(parts) == 3 and all(parts) and parts[0] != "secret":
            out.add((parts[0], parts[1], parts[2]))
    return out


def _glob_re(glob: str) -> re.Pattern[str]:
    """gitignore-ish glob: `**` crosses `/`, `*`/`?` do not; `**/` also matches nothing;
    a pattern with no `/` matches at any depth; a trailing `/**` or `/` matches the tree.
    Case-insensitive (so `.PDF` can't slip past `*.pdf`)."""
    g = glob.strip().lstrip("/")
    if g.endswith("/"):
        g += "**"
    if "/" not in g:
        g = "**/" + g
    out, i = "", 0
    while i < len(g):
        if g.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif g.startswith("**", i):
            out += ".*"
            i += 2
        elif g[i] == "*":
            out += "[^/]*"
            i += 1
        elif g[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(g[i])
            i += 1
    return re.compile(out + r"\Z", re.IGNORECASE | re.DOTALL)


def data_file_hits(added_paths: list[str], data_globs: list[str] | None,
                   fixture_dir: str | None) -> list[Hit]:
    """Document-repo guard: a newly ADDED file matching a data glob that is not under
    `fixture_dir` is a hit. Location is the index only — the path is never named.
    With data_globs set but no fixture_dir, nothing is exempt (fail closed)."""
    pats = [_glob_re(g) for g in (data_globs or []) if isinstance(g, str) and g.strip()]
    if not pats:
        return []
    fx = (fixture_dir or "").strip().replace("\\", "/").strip("/") if isinstance(
        fixture_dir, str) else ""
    hits: list[Hit] = []
    for i, p in enumerate(added_paths):
        norm = p.replace("\\", "/")
        parts = norm.split("/")
        in_fixture = bool(fx) and ".." not in parts and norm.lower().startswith(fx.lower() + "/")
        if not in_fixture and any(r.match(norm) for r in pats):
            hits.append(Hit("data-file", f"data:{i}"))
    return hits


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

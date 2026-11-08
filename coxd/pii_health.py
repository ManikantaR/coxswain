"""PII denylist health. Returns only booleans and counts; never prints or logs terms.

The owner's review tool writes metadata into `COXD_HOME/pii/`: `last-reviewed` (epoch
seconds), `new-candidates.count` and `harvest-failed.count` (integers). We read those,
and parse the denylist in-process (same parser as the scanner) only to count effective
terms, so a missing, empty or comment-only file is `blocked`. Contents never leave
this function.
"""
from __future__ import annotations

import math
import os
import time

import registry

DUE_DAYS = 30
OVERDUE_DAYS = 45
PING_EVERY_DAYS = 3


def _read_num(name: str) -> float | None:
    try:
        v = float((registry.home() / "pii" / name).read_text().strip())
    except (OSError, ValueError):
        return None
    return v if math.isfinite(v) else None


def status(now: float | None = None) -> dict:
    import pii  # late import
    now = time.time() if now is None else now
    # Count effective terms in-process only (same parser as the scanner, so a
    # comment-only or blank file is "empty"); contents are never returned or logged.
    try:
        terms, missing = pii.load_denylist()
    except OSError:
        terms, missing = [], True
    exists = not missing
    empty = exists and not terms
    ts = _read_num("last-reviewed")
    age = None if ts is None else max(0, int((now - ts) // 86400))
    n = _read_num("new-candidates.count")
    cands = 0 if n is None or n < 0 else int(n)
    hf = _read_num("harvest-failed.count")
    failed = 0 if hf is None or hf < 0 else int(hf)
    if not exists or empty:
        level = "blocked"
    elif age is None or age > OVERDUE_DAYS:
        level = "overdue"  # never reviewed counts as overdue
    elif age > DUE_DAYS or cands > 0 or failed > 0:
        level = "due"
    else:
        level = "ok"
    return {"exists": exists, "empty": empty, "age_days": age,
            "new_candidates": cands, "harvest_failed": failed, "level": level}


def summary_line(st: dict | None = None) -> str | None:
    """One counts-only line, or None when ok."""
    st = st or status()
    if st["level"] == "ok":
        return None
    if st["level"] == "blocked":
        why = "missing" if not st["exists"] else "empty"
        return f"PII denylist: {why} — intake and PRs for protected repos are blocked"
    age = ("never reviewed" if st["age_days"] is None
           else f"reviewed {st['age_days']} days ago")
    extra = (f", {st['harvest_failed']} sources failed"
             if st.get("harvest_failed") else "")
    return (f"PII denylist: {age}, {st['new_candidates']} new candidates{extra}"
            " — run the review")


def pill(st: dict | None = None) -> dict:
    st = st or status()
    if not st["exists"] or st["empty"]:
        text = "denylist " + ("missing" if not st["exists"] else "empty")
    elif st["age_days"] is None:
        text = "denylist unreviewed"
    else:
        text = f"denylist {st['age_days']}d"
    return {"text": text, "level": st["level"]}


def _marker():
    return registry.home() / "pii-ping-last"


def ping_due(now_local, st: dict | None = None) -> bool:
    """1st of each month (once that day), then every 3 days while not ok.
    A review landing (level ok) stops the 3-day cadence."""
    st = st or status()
    hh, mm = (int(x) for x in os.environ.get("COXD_DIGEST_AT", "07:20").split(":"))
    if (now_local.hour, now_local.minute) < (hh, mm):
        return False  # not silently overnight
    try:
        last = float(_marker().read_text().strip())
    except (OSError, ValueError):
        last = None
    now = now_local.timestamp()
    if now_local.day == 1:
        if last is None:
            return True
        from datetime import datetime
        return datetime.fromtimestamp(last, now_local.tzinfo).date() != now_local.date()
    if st["level"] == "ok":
        return False
    return last is None or now - last >= PING_EVERY_DAYS * 86400 - 3600


def ping_text(st: dict | None = None) -> str:
    st = st or status()
    return summary_line(st) or (
        f"PII denylist: reviewed {st['age_days']} days ago, "
        f"{st['new_candidates']} new candidates — monthly check")


def maybe_ping(now_local, send) -> bool:
    st = status(now_local.timestamp())
    if not ping_due(now_local, st):
        return False
    m = _marker()
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(str(now_local.timestamp()))
    send("coxd · PII denylist", ping_text(st))
    return True

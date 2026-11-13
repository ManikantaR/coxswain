"""Monthly AI-spend alert. An alert only — work is never stopped.

COXD_MONTHLY_SPEND_ALERT_USD (unset/blank/invalid/<=0 = off). Month-to-date spend is
summed from the timestamped cost_ledger since local (COXD_TZ) midnight on the 1st.
The first pass over the limit in a calendar month sends ONE message through the
same channel as the digest (notify) and drops a marker in COXD_HOME; the next
month has a new marker name, so it re-arms on the 1st by itself.
"""
from __future__ import annotations

import math
import os

import notify
import registry
import store


def limit_usd() -> float | None:
    raw = (os.environ.get("COXD_MONTHLY_SPEND_ALERT_USD") or "").strip()
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if math.isfinite(v) and v > 0 else None


def month_to_date(now_local=None) -> float:
    now_local = now_local or notify.local_now()
    start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with store._conn() as c:
        r = c.execute("SELECT COALESCE(SUM(delta),0) FROM cost_ledger WHERE ts>=?",
                      (start.timestamp(),)).fetchone()
    return float(r[0] or 0)


def _marker(now_local):
    return registry.home() / f"spend-alert-{now_local:%Y-%m}"


def banner_text(mtd: float, limit: float) -> str:
    return f"Spend this month ${mtd:.2f}, over your ${limit:.2f} alert"


def status(now_local=None) -> dict:
    """For the board header: always the month-to-date; banner only while over the limit."""
    now_local = now_local or notify.local_now()
    mtd, limit = month_to_date(now_local), limit_usd()
    over = limit is not None and mtd > limit
    return {"mtd": round(mtd, 2), "limit": limit, "over": over,
            "banner": banner_text(mtd, limit) if over else None}


def maybe_alert(now_local=None) -> bool:
    """Send the once-per-month alert if the limit is passed. True when sent."""
    now_local = now_local or notify.local_now()
    limit = limit_usd()
    if limit is None:
        return False
    mtd = month_to_date(now_local)
    if mtd <= limit:
        return False
    m = _marker(now_local)
    if m.exists():
        return False
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(f"{mtd:.2f}")
    for old in registry.home().glob("spend-alert-*"):  # keep the folder tidy
        if old != m:
            old.unlink(missing_ok=True)
    notify.notify_async("coxd · monthly spend alert", banner_text(mtd, limit)
                        + ". Work continues — this is an alert, not a stop.")
    return True

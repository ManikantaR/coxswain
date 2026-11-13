"""Monthly spend alert: off when unset, silent below limit, once when crossed, re-arms next month.

    cd coxd && ../.venv/bin/python test_spend.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-spend-")

import notify  # noqa: E402
import spend  # noqa: E402
import store  # noqa: E402

fails = 0
sent: list[tuple] = []
notify.notify_async = lambda title, msg, priority="default": sent.append((title, msg))


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


ET = ZoneInfo("America/New_York")
NOV = datetime(2026, 11, 12, 9, 0, tzinfo=ET)
DEC = datetime(2026, 12, 2, 9, 0, tzinfo=ET)


def ledger(when: datetime, delta: float) -> None:
    with store._conn() as c:
        c.execute("INSERT INTO cost_ledger(task_id,ts,delta) VALUES(?,?,?)",
                  ("t-demo", when.timestamp(), delta))


ledger(datetime(2026, 10, 31, 23, 0, tzinfo=ET), 500.0)  # last month: never counts in Nov
ledger(datetime(2026, 11, 3, 10, 0, tzinfo=ET), 6.0)

os.environ.pop("COXD_MONTHLY_SPEND_ALERT_USD", None)
ledger(datetime(2026, 11, 4, 10, 0, tzinfo=ET), 100.0)
check("unset → off", not spend.maybe_alert(NOV) and not sent)
check("unset → no banner", spend.status(NOV)["banner"] is None)
check("mtd shown even when off", spend.status(NOV)["mtd"] == 106.0)
for bad in ("", "abc", "0", "-5", "nan"):
    os.environ["COXD_MONTHLY_SPEND_ALERT_USD"] = bad
    check(f"invalid {bad!r} → off", not spend.maybe_alert(NOV))

store._conn().execute("DELETE FROM cost_ledger WHERE delta=100.0").connection.commit()
os.environ["COXD_MONTHLY_SPEND_ALERT_USD"] = "10"
check("below limit → no alert", not spend.maybe_alert(NOV) and not sent)
check("below limit → no banner", spend.status(NOV)["banner"] is None)
ledger(datetime(2026, 11, 5, 10, 0, tzinfo=ET), 5.0)
check("crossed → alert sent", spend.maybe_alert(NOV) and len(sent) == 1)
check("message has spend and limit", "$11.00" in sent[0][1] and "$10.00" in sent[0][1])
check(
    "banner shown",
    spend.status(NOV)["banner"] == "Spend this month $11.00, over your $10.00 alert",
)
ledger(datetime(2026, 11, 6, 10, 0, tzinfo=ET), 5.0)
check("still over → no second alert", not spend.maybe_alert(NOV) and len(sent) == 1)
check("December starts at zero", spend.status(DEC)["mtd"] == 0 and not spend.maybe_alert(DEC))
ledger(datetime(2026, 12, 1, 12, 0, tzinfo=ET), 20.0)
check("next month → alerts again", spend.maybe_alert(DEC) and len(sent) == 2)
check("next month → once", not spend.maybe_alert(DEC) and len(sent) == 2)

print("OK" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)

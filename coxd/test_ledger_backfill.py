"""cost_ledger backfill: result events from before the ledger existed get rows, exactly once.

    cd coxd && python test_ledger_backfill.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-backfill-")

import store  # noqa: E402

c = store._conn()


def ev(task, ts, kind, data):
    c.execute("INSERT INTO events(task_id,ts,kind,data) VALUES(?,?,?,?)",
              (task, ts, kind, json.dumps(data)))


def total():
    return c.execute("SELECT COALESCE(SUM(delta),0) FROM cost_ledger").fetchone()[0]


ev("t1", 10.0, "result", {"cost": 1.5})
ev("t1", 11.0, "result", {"cost": 2.0})
ev("t1", 12.0, "result", {"cost": None})        # non-numeric: skipped
ev("t1", 13.0, "state", {"state": "working"})   # not a result: skipped
c.commit()
assert store.backfill_ledger(c) == 2 and abs(total() - 3.5) < 1e-9
assert store.backfill_ledger(c) == 0 and abs(total() - 3.5) < 1e-9  # second run: nothing

# a live ledger row appears later; its own event is not double counted
store.add_cost("t2", 4.0)
ev("t2", 1e12, "result", {"cost": 4.0})
c.commit()
assert store.backfill_ledger(c) == 0 and abs(total() - 7.5) < 1e-9
print("ok")

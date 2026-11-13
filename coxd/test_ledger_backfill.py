"""cost_ledger backfill: events from before the ledger existed get rows, exactly once.

    cd coxd && python test_ledger_backfill.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-backfill-")

import sqlite3  # noqa: E402

import store  # noqa: E402

c = store._conn()  # fresh db: marker set, nothing to backfill
c.execute("DELETE FROM meta")  # simulate a db that predates the migration
c.commit()


def ev(task, ts, kind, data):
    c.execute("INSERT INTO events(task_id,ts,kind,data) VALUES(?,?,?,?)",
              (task, ts, kind, json.dumps(data)))


def total():
    return c.execute("SELECT COALESCE(SUM(delta),0) FROM cost_ledger").fetchone()[0]


ev("t1", 10.0, "result", {"cost": 1.5})
ev("t1", 11.0, "result", {"cost": 2.0})
ev("t1", 12.0, "result", {"cost": None})        # non-numeric: skipped
ev("t1", 13.0, "state", {"state": "working"})   # not a result: skipped
# first ledger row (ts 1000) whose result event was emitted 2s earlier -> not re-added
c.execute("INSERT INTO cost_ledger(task_id,ts,delta) VALUES('t2',1000.0,4.0)")
ev("t2", 998.0, "result", {"cost": 4.0})
c.commit()
assert store.backfill_ledger(c) == 2 and abs(total() - 7.5) < 1e-9, total()
assert store.backfill_ledger(c) == 0 and abs(total() - 7.5) < 1e-9  # marker: nothing

# a second connection (another process) opening afterwards adds nothing either
c2 = store._conn()
assert store.backfill_ledger(c2) == 0 and abs(total() - 7.5) < 1e-9

# concurrent first-openers: one wins the write lock, the other sees the marker
c.execute("DELETE FROM meta")
c.execute("DELETE FROM cost_ledger")
c.commit()
c3 = sqlite3.connect(store.db_path())
c3.execute("BEGIN IMMEDIATE")  # holds the write lock; c2 must wait/fail, not double insert
try:
    c2.execute("PRAGMA busy_timeout=100")
    store.backfill_ledger(c2)
    raise SystemExit("expected lock contention")
except sqlite3.OperationalError:
    pass
c3.rollback()
c3.close()
n = store.backfill_ledger(c2) + store.backfill_ledger(c)
assert n == 3 and abs(total() - 7.5) < 1e-9, (n, total())
print("ok")

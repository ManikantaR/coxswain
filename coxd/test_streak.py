"""Tests for greenlight tier storage + per-repo clean-PR streak.

    cd coxd && .venv/bin/python test_streak.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-streak-")

import landed  # noqa: E402
import store  # noqa: E402

res = []


def check(name, cond):
    res.append(cond)
    print(("PASS" if cond else "FAIL"), name)


BOTS = ["coxd"]
store.create_task("t1", "acme-widgets", "brief", "/tmp/wt")
store.set_greenlight_tier("t1", "GREEN")
check("tier stored", store.get_task("t1")["greenlight_tier"] == "GREEN")

check("streak starts 0", store.get_clean_streak("acme-widgets") == 0)
clean = landed.is_clean_merge("GREEN", True, ["coxd", "coxd"], BOTS)
check("clean merge is clean", clean)
store.record_merge("acme-widgets", clean)
store.record_merge("acme-widgets", clean)
check("streak up to 2", store.get_clean_streak("acme-widgets") == 2)
check("other repo independent", store.get_clean_streak("zeta-gizmos") == 0)

check("human commit not clean", not landed.is_clean_merge("GREEN", True, ["coxd", "bob-example"], BOTS))
check("AMBER not clean", not landed.is_clean_merge("AMBER", True, ["coxd"], BOTS))
check("red CI not clean", not landed.is_clean_merge("GREEN", False, ["coxd"], BOTS))
check("no commits info not clean", not landed.is_clean_merge("GREEN", True, [], BOTS))
check("no tier not clean", not landed.is_clean_merge(None, True, ["coxd"], BOTS))

store.record_merge("acme-widgets", landed.is_clean_merge("GREEN", True, ["bob-example"], BOTS))
check("non-clean resets to 0", store.get_clean_streak("acme-widgets") == 0)
store.record_merge("acme-widgets", True)
check("streak restarts at 1", store.get_clean_streak("acme-widgets") == 1)

sys.exit(0 if all(res) else 1)

"""Unit test for greenlight.tier — the auto-greenlight risk classifier.

Standalone: cd coxd && .venv/bin/python test_greenlight.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import greenlight  # noqa: E402

SACRED = ["web/src/app/page.tsx", "backend/db_service.py", ".env"]
res = []


def check(name, cond):
    res.append(cond)
    print(("PASS" if cond else "FAIL"), name)


# GREEN: small, clean, no sacred, no findings
t, _ = greenlight.tier([], "approve", ["backend/x.py"], 10, 2, SACRED)
check("clean small → GREEN", t == "GREEN")

# RED: sacred path touched
t, _ = greenlight.tier([], "approve", ["web/src/app/page.tsx"], 5, 1, SACRED)
check("sacred path → RED", t == "RED")

# RED: high-severity finding
t, _ = greenlight.tier([{"severity": "high", "summary": "x"}], "approve",
                       ["backend/x.py"], 3, 1, SACRED)
check("high finding → RED", t == "RED")

# RED: verdict reject
t, _ = greenlight.tier([], "reject", ["backend/x.py"], 2, 1, SACRED)
check("verdict reject → RED", t == "RED")

# AMBER: medium finding, otherwise clean
t, _ = greenlight.tier([{"severity": "med", "summary": "y"}], "approve",
                       ["backend/x.py"], 2, 1, SACRED)
check("med finding → AMBER", t == "AMBER")

# AMBER: large diff (files)
t, _ = greenlight.tier([], "approve", [f"f{i}.py" for i in range(8)], 10, 5, SACRED)
check("many files → AMBER", t == "AMBER")

# AMBER: large diff (lines)
t, _ = greenlight.tier([], "approve", ["big.py"], 200, 50, SACRED)
check("many lines → AMBER", t == "AMBER")

# header renders + ranks high before low
h = greenlight.header("RED", ["touches sacred path"],
                      [{"severity": "low", "file": "a", "line": 1, "summary": "lo"},
                       {"severity": "high", "file": "b", "line": 2, "summary": "hi"}])
check("header ranks high first", h.index("hi") < h.index("lo") and "RED" in h)

print("\nALL PASS ✓" if all(res) else "\nSOME FAILED ✗")
sys.exit(0 if all(res) else 1)

"""rereview role: resolution + defaults (follows the resolved review role)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for k in list(os.environ):
    if k.startswith("COXD_") and ("MODEL" in k or "EFFORT" in k):
        del os.environ[k]

import roles  # noqa: E402

d = roles.resolve(None)
assert d["rereview_model"] == d["review_model"] == roles.DEFAULTS["review_model"]
assert d["rereview_effort"] == d["review_effort"]

d = roles.resolve(None, review_model="m-x", review_effort="high")
assert d["rereview_model"] == "m-x" and d["rereview_effort"] == "high"

d = roles.resolve({"review_model": "m-r", "rereview_model": "m-rr", "rereview_effort": "low"})
assert (d["review_model"], d["rereview_model"], d["rereview_effort"]) == ("m-r", "m-rr", "low")

d = roles.resolve({"review_model": "m-r"}, rereview_model="m-arg")
assert d["rereview_model"] == "m-arg"

os.environ["COXD_REREVIEW_MODEL"] = "m-env"
os.environ["COXD_REREVIEW_EFFORT"] = "max"
d = roles.resolve(None)
assert d["rereview_model"] == "m-env" and d["rereview_effort"] == "max"
assert d["review_model"] == roles.DEFAULTS["review_model"]  # review untouched
del os.environ["COXD_REREVIEW_MODEL"], os.environ["COXD_REREVIEW_EFFORT"]

d = roles.resolve({"rereview_effort": "bogus", "review_effort": "high"})
assert d["rereview_effort"] == "high"  # invalid ignored -> falls back to review effort
print("ok test_roles_rereview")

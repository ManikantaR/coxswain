"""Standalone test for spend + result payloads. Run: python test_stepspend.py"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lane  # noqa: E402
import stepspend as spend  # noqa: E402

assert spend.short_name("claude-sonnet-5-5") == "Sonnet 5.5"
assert spend.short_name("claude-opus-4-1-20250805") == "Opus 4.1"
assert spend.short_name("claude-opus-5") == "Opus 5"
assert spend.short_name("claude-3-5-haiku-20241022") == "Haiku 3.5"
assert spend.short_name("weird") == "weird" and spend.short_name(None) == "unknown"

r = SimpleNamespace(total_cost_usd=1.5, num_turns=3, stop_reason="end_turn",
                    usage={"input_tokens": 1}, model_usage={"claude-sonnet-5-5": {"costUSD": 1.5}})
p = lane.result_payload(r, "claude-sonnet-5-5")
assert p["model"] == "claude-sonnet-5-5" and p["model_usage"] == r.model_usage and p["usage"] == r.usage
assert p["cost"] == 1.5 and p["num_turns"] == 3 and p["stop_reason"] == "end_turn"


def st(ts, s):
    return {"kind": "state", "ts": ts, "data": {"state": s}}


def res(ts, **d):
    return {"kind": "result", "ts": ts, "data": d}


new = [st(10, "working"),
       res(11, model="claude-sonnet-5-5", cost=2.0,
           model_usage={"claude-sonnet-5-5": {"costUSD": 1.5}, "claude-haiku-4-5": {"costUSD": 0.5}}),
       st(12, "reviewing"),
       res(13, model="claude-opus-5", cost=3.0, model_usage={"claude-opus-5": {"costUSD": 3.0}}),
       st(14, "fixing"), res(15, model="claude-sonnet-5-5", cost=1.0,
                             model_usage={"claude-sonnet-5-5": {"costUSD": 1.0}})]
old = [st(10, "working"), res(11, cost=4.0, num_turns=5), res(1, cost=9.0)]
assert spend.stage_models(new) == {0: "Sonnet 5.5", 2: "Opus 5"}
assert spend.stage_models(old) == {}
a = spend.aggregate([new, old], since=5)
assert a["by_model"] == {"Sonnet 5.5": 2.5, "Haiku 4.5": 0.5, "Opus 5": 3.0, "unknown": 4.0}, a
a = spend.aggregate([new, old], since=10)  # old ts=1 event excluded
assert a["by_model"]["unknown"] == 4.0 and a["by_phase"] == {"implement": 6.0, "fix": 1.0, "review": 3.0}, a
assert abs(a["total"] - 10.0) < 1e-9
assert spend.month_start(1767225600 + 86400 * 20, "UTC") == 1767225600  # 2026-01-01
print("ok")

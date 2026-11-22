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
assert p["model"] == "claude-sonnet-5-5" and p["model_usage"] == r.model_usage
assert p["usage"] == r.usage
assert p["cost"] == 1.5 and p["num_turns"] == 3 and p["stop_reason"] == "end_turn"


def st(ts, s):
    return {"kind": "state", "ts": ts, "data": {"state": s}}


def res(ts, **d):
    return {"kind": "result", "ts": ts, "data": d}


new = [st(10, "working"),
       res(11, model="claude-sonnet-5-5", cost=2.0,
           model_usage={"claude-sonnet-5-5": {"costUSD": 1.5},
                        "claude-haiku-4-5": {"costUSD": 0.5}}),
       st(12, "reviewing"),
       res(13, model="claude-opus-5", cost=3.0, model_usage={"claude-opus-5": {"costUSD": 3.0}}),
       st(14, "fixing"), res(15, model="claude-sonnet-5-5", cost=1.0,
                             model_usage={"claude-sonnet-5-5": {"costUSD": 1.0}})]
old = [st(10, "working"), res(11, cost=4.0, num_turns=5), res(1, cost=9.0)]
assert spend.stage_models(new) == {0: "Sonnet 5.5", 2: "Opus 5"}
assert spend.stage_models(old) == {}
a = spend.aggregate([new, old], since=5)
assert a["by_model"] == {"Sonnet 5.5": 2.5, "Haiku 4.5": 0.5, "Opus 5": 3.0,
                         "unknown": 4.0}, a
a = spend.aggregate([new, old], since=10)  # old ts=1 event excluded
assert a["by_model"]["unknown"] == 4.0, a
assert a["by_phase"] == {"plan": 0.0, "implement": 6.0, "fix": 1.0, "review": 3.0}, a
assert abs(a["total"] - 10.0) < 1e-9
assert spend.month_start(1767225600 + 86400 * 20, "UTC") == 1767225600  # 2026-01-01
# panel total == ledger total; the gap is surfaced in both breakdowns, never negative
evs = [st(10, "working"), res(11, model="claude-sonnet-5-5", cost=2.0)]
m = spend.month_spend([evs], "UTC", now=100.0, ledger_total=7.0)
assert m["total"] == 7.0 and abs(m["by_model"]["unattributed"] - 5.0) < 1e-9, m
assert abs(m["by_phase"]["unattributed"] - 5.0) < 1e-9, m
assert abs(sum(m["by_phase"].values()) - m["total"]) < 1e-9, m
m = spend.month_spend([evs], "UTC", now=100.0, ledger_total=2.0)
assert m["total"] == 2.0 and "unattributed" not in m["by_model"], m
m = spend.month_spend([evs], "UTC", now=100.0, ledger_total=1.0)
assert m["total"] == 1.0 and "unattributed" not in m["by_model"], m
assert abs(m["events_exceed_ledger"] - 1.0) < 1e-9, m

# Result event without model/model_usage appears under "before model tracking", never "unknown"
m_old = spend.month_spend([old], "UTC", now=100.0)
assert spend.UNKNOWN_LABEL == "before model tracking"
assert "unknown" not in m_old["by_model"], m_old
assert abs(m_old["by_model"][spend.UNKNOWN_LABEL] - 13.0) < 1e-9, m_old
assert m_old["unknown_tip"] == spend.UNKNOWN_TIP

# Per-phase bars (plan/implement/fix/review) sum to total for month and all-time, per lane
jan_start = 1767225600.0  # 2026-01-01 00:00 UTC
now_jan = jan_start + 15 * 86400
claude_evs = [
    st(jan_start - 1000, "planning"), res(jan_start - 900, model="claude-opus-5", cost=0.5),
    st(jan_start + 10, "planning"), res(jan_start + 20, model="claude-opus-5", cost=0.4),
    st(jan_start + 30, "working"), res(jan_start + 40, model="claude-sonnet-5-5", cost=1.6),
    st(jan_start + 50, "fixing"), res(jan_start + 60, model="claude-sonnet-5-5", cost=0.8),
    st(jan_start + 70, "reviewing"), res(jan_start + 80, model="claude-opus-5", cost=1.2),
]
agy_evs = [
    st(jan_start - 500, "planning"), res(jan_start - 400, model="claude-opus-5", cost=0.3),
    st(jan_start + 15, "planning"), res(jan_start + 25, model="claude-opus-5", cost=0.25),
    st(jan_start + 75, "reviewing"), res(jan_start + 85, model="claude-opus-5", cost=0.75),
]
pb_month = spend.phase_spend([claude_evs, agy_evs], spend.month_start(now_jan, "UTC"),
                             lanes=["claude", "agy-handoff"])
pb_all = spend.phase_spend([claude_evs, agy_evs], 0.0, lanes=["claude", "agy-handoff"])
for pb in (pb_month, pb_all):
    for ln in ("claude", "agy"):
        bp = pb[ln]["by_phase"]
        assert tuple(bp.keys()) == ("plan", "implement", "fix", "review"), bp
        assert abs(sum(bp.values()) - pb[ln]["total"]) < 1e-9, (ln, pb[ln])
assert abs(pb_month["claude"]["total"] - 4.0) < 1e-9, pb_month
assert abs(pb_all["claude"]["total"] - 4.5) < 1e-9, pb_all
assert abs(pb_month["agy"]["total"] - 1.0) < 1e-9, pb_month
assert abs(pb_all["agy"]["total"] - 1.3) < 1e-9, pb_all
print("ok")

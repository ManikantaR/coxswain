"""Pure helpers for the board's since-start scorecard, weekly trend, and budget check."""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NEEDS_YOU = frozenset({"pr_ready", "needs_human", "waiting_agy", "needs_choice"})
_LANE_ORDER = {"claude": 0, "agy": 1}


def _lane_of(task: dict) -> str:
    return "agy" if task.get("lane") in ("agy", "agy-handoff") else "claude"


def _num_cost(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) and f > 0 else None


def _events_for(tasks: list[dict], events_by_task: dict[str, list[dict]] | list[list[dict]]
                ) -> list[list[dict]]:
    if isinstance(events_by_task, dict):
        return [events_by_task.get(t["id"], []) for t in tasks]
    return [events_by_task[i] if i < len(events_by_task) else [] for i in range(len(tasks))]


def _is_blocking_finding(f: object) -> bool:
    if not isinstance(f, dict):
        return False
    sev = str(f.get("severity") or "").lower()
    pillar = str(f.get("pillar") or "").lower()
    return sev in ("high", "med", "medium", "critical", "major") and pillar != "advisory"


def _first_review_passed(evs: list[dict]) -> bool | None:
    """Return True/False from the first `review` event, or None if never reviewed."""
    for e in evs:
        if e.get("kind") == "review" and isinstance(e.get("data"), dict):
            d = e["data"]
            if d.get("outcome") == "review-error":
                return False
            verdict = str(d.get("verdict") or "").lower()
            if verdict in ("approve", "approved", "pass", "passed"):
                findings = d.get("findings") or []
                if isinstance(findings, list) and any(_is_blocking_finding(f) for f in findings):
                    return False
                return True
            return False
    return None


def _first_gate_passed(evs: list[dict]) -> bool | None:
    """Return True/False from the first `gate` event, or None if never gated."""
    for e in evs:
        if e.get("kind") == "gate" and isinstance(e.get("data"), dict):
            return e["data"].get("passed") is True
    return None


def _summarize(repo: str, lane: str, pairs: list[tuple[dict, list[dict]]],
               *, is_total: bool, streak: tuple[int, int] | None) -> dict:
    landed = sum(1 for t, _ in pairs if t.get("state") == "landed")
    needs_you = sum(1 for t, _ in pairs if t.get("state") in NEEDS_YOU)

    fr_pass = fr_tot = 0
    fg_pass = fg_tot = 0
    for _, evs in pairs:
        fr = _first_review_passed(evs)
        if fr is not None:
            fr_tot += 1
            fr_pass += int(fr)
        fg = _first_gate_passed(evs)
        if fg is not None:
            fg_tot += 1
            fg_pass += int(fg)

    # Claude USD costs only (agy tokens are not dollars).
    if lane == "agy":
        costs: list[float] = []
    else:
        costs = []
        for t, _ in pairs:
            if _lane_of(t) != "claude":
                continue
            c = _num_cost(t.get("cost"))
            if c is not None:
                costs.append(c)

    median_cost = round(statistics.median(costs), 4) if costs else None
    total_cost = round(sum(costs), 4) if costs else None

    return {
        "repo": repo,
        "lane": lane,
        "is_total": is_total,
        "tasks": len(pairs),
        "landed": landed,
        "needs_you": needs_you,
        "first_review_pct": round(100.0 * fr_pass / fr_tot, 1) if fr_tot else None,
        "first_review_passed": fr_pass,
        "first_review_total": fr_tot,
        "first_gate_pct": round(100.0 * fg_pass / fg_tot, 1) if fg_tot else None,
        "first_gate_passed": fg_pass,
        "first_gate_total": fg_tot,
        "median_cost": median_cost,
        "total_cost": total_cost,
        "streak_current": streak[0] if streak is not None else None,
        "streak_best": streak[1] if streak is not None else None,
    }


def build(tasks: list[dict],
          events_by_task: dict[str, list[dict]] | list[list[dict]],
          streaks: dict[str, tuple[int, int]] | None = None) -> list[dict]:
    """Build since-start scorecard rows per (repo, lane) plus an all-repos summary row."""
    streaks = streaks or {}
    ev_lists = _events_for(tasks, events_by_task)
    all_pairs = list(zip(tasks, ev_lists, strict=True))

    groups: dict[tuple[str, str], list[tuple[dict, list[dict]]]] = {}
    for t, evs in all_pairs:
        key = (str(t.get("repo") or ""), _lane_of(t))
        groups.setdefault(key, []).append((t, evs))

    rows: list[dict] = []
    for repo, lane in sorted(groups, key=lambda k: (k[0], _LANE_ORDER.get(k[1], 9))):
        cur, best = streaks.get(repo, (0, 0))
        rows.append(_summarize(repo, lane, groups[(repo, lane)],
                               is_total=False, streak=(cur, best)))

    rows.append(_summarize("all", "all", all_pairs, is_total=True, streak=None))
    return rows


def _landed_ts(evs: list[dict]) -> float | None:
    ts: float | None = None
    for e in evs:
        if e.get("kind") == "state" and isinstance(e.get("data"), dict):
            if e["data"].get("state") == "landed":
                raw = e.get("ts")
                if (not isinstance(raw, bool) and isinstance(raw, (int, float))
                        and math.isfinite(raw)):
                    ts = float(raw)
    return ts


def weekly_cost_per_landed(
    tasks: list[dict],
    events: dict[str, list[dict]] | list[list[dict]],
    now: float,
    tz: str = "UTC",
    weeks: int = 8,
) -> list[float | None]:
    """Return `weeks` weekly values (oldest to newest): Claude cost divided by Claude tasks
    landed in that week, or None when 0 landed. Bucketed by the `landed` state event `ts`."""
    if weeks <= 0:
        return []
    try:
        z = ZoneInfo(tz)
    except Exception:
        z = ZoneInfo("UTC")

    now_date = datetime.fromtimestamp(now, z).date()
    cur_monday = now_date - timedelta(days=now_date.weekday())

    bucket_cost = [0.0] * weeks
    bucket_landed = [0] * weeks
    ev_lists = _events_for(tasks, events)

    for t, evs in zip(tasks, ev_lists, strict=True):
        if _lane_of(t) != "claude" or t.get("state") != "landed":
            continue
        lts = _landed_ts(evs)
        if lts is None:
            continue
        ev_date = datetime.fromtimestamp(lts, z).date()
        ev_monday = ev_date - timedelta(days=ev_date.weekday())
        weeks_ago = (cur_monday - ev_monday).days // 7
        if 0 <= weeks_ago < weeks:
            idx = weeks - 1 - weeks_ago
            bucket_landed[idx] += 1
            c = _num_cost(t.get("cost"))
            if c is not None:
                bucket_cost[idx] += c

    return [
        round(bucket_cost[i] / bucket_landed[i], 4) if bucket_landed[i] > 0 else None
        for i in range(weeks)
    ]


def over_budget(cost: float | None, threshold: float | None) -> bool:
    """True when numeric `cost` strictly exceeds positive numeric `threshold`."""
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return False
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return False
    if not (math.isfinite(cost) and math.isfinite(threshold)) or threshold <= 0:
        return False
    return float(cost) > float(threshold)

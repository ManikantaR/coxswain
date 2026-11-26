"""Pure aggregation helpers for the agy vs claude comparison panel and graduation hint."""
from __future__ import annotations

import math
import statistics

import stepspend

LANES = ("claude", "agy")
SIZES = ("small", "medium")
NEEDS_YOU = frozenset({"waiting_agy", "needs_choice", "pr_ready", "needs_human"})
GRADUATION_NEED = 5
GRADUATION_PASS = 4
SPARK_LIMIT = 10

_SEV_MAP = {
    "high": "critical",
    "critical": "critical",
    "med": "major",
    "medium": "major",
    "major": "major",
    "low": "minor",
    "minor": "minor",
}
_BLOCKING_SEV = frozenset({"high", "med", "medium", "critical", "major"})
_APPROVE_VERDICTS = frozenset({"approve", "approved", "pass", "passed"})


def _finite_num(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _size_of_task(task: dict) -> str | None:
    s = task.get("size")
    return s if s in SIZES else None


def _is_blocking_finding(f: object) -> bool:
    if not isinstance(f, dict):
        return False
    sev = str(f.get("severity") or "").lower()
    pillar = str(f.get("pillar") or "").lower()
    return sev in _BLOCKING_SEV and pillar != "advisory"


def _review_approved(data: dict) -> bool:
    if data.get("outcome") == "review-error":
        return False
    verdict = str(data.get("verdict") or "").lower()
    if verdict not in _APPROVE_VERDICTS:
        return False
    findings = data.get("findings") or []
    if isinstance(findings, list) and any(_is_blocking_finding(f) for f in findings):
        return False
    return True


def _has_cap_stop(task: dict, evs: list[dict]) -> bool:
    reason = task.get("reason")
    if isinstance(reason, str) and reason.endswith("-cap"):
        return True
    for e in evs:
        kind = e.get("kind")
        if isinstance(kind, str) and kind.endswith("-cap"):
            return True
        if kind == "state" and isinstance(e.get("data"), dict):
            r = e["data"].get("reason")
            if isinstance(r, str) and r.endswith("-cap"):
                return True
    return False


def _human_minutes(evs: list[dict]) -> float:
    """Sum intervals from entering a NEEDS_YOU state to the next state event leaving it.
    Any interval still open at the end closes at the landed or last event timestamp so
    results stay deterministic."""
    entered_ts: float | None = None
    total_s = 0.0
    last_ts: float | None = None
    landed_ts: float | None = None
    for e in evs:
        ts = _finite_num(e.get("ts"))
        if ts is not None:
            last_ts = ts if last_ts is None else max(last_ts, ts)
        if e.get("kind") != "state" or not isinstance(e.get("data"), dict):
            continue
        st = e["data"].get("state")
        if st == "landed" and ts is not None:
            landed_ts = ts
        if st in NEEDS_YOU:
            if entered_ts is None and ts is not None:
                entered_ts = ts
        elif entered_ts is not None and ts is not None:
            total_s += max(0.0, ts - entered_ts)
            entered_ts = None
    if entered_ts is not None:
        end_ts = landed_ts if landed_ts is not None else last_ts
        if end_ts is not None:
            total_s += max(0.0, end_ts - entered_ts)
    return round(total_s / 60.0, 4)


def _events_for(
    tasks: list[dict],
    events_by_task: dict[str, list[dict]] | list[list[dict]],
) -> list[list[dict]]:
    if isinstance(events_by_task, dict):
        return [events_by_task.get(t["id"], []) for t in tasks]
    return [events_by_task[i] if i < len(events_by_task) else [] for i in range(len(tasks))]


def task_record(task: dict, evs: list[dict]) -> dict:
    """Derive a per-task comparison record from a task row and its event list (pure)."""
    lane = stepspend.normalize_lane(task.get("lane"))
    size = _size_of_task(task)

    plan_cost = 0.0
    review_cost = 0.0
    for phase, e in stepspend.phased_results(evs):
        c = sum(stepspend.result_costs(e["data"]).values())
        if phase == "plan":
            plan_cost += c
        elif phase == "review":
            review_cost += c
    plan_cost = round(plan_cost, 6)
    review_cost = round(review_cost, 6)

    if lane == "agy":
        claude_cost = round(plan_cost + review_cost, 6)
    else:
        raw_cost = _finite_num(task.get("cost"))
        claude_cost = round(raw_cost, 6) if raw_cost is not None and raw_cost >= 0 else 0.0

    gate_first_pass: bool | None = None
    gate_reds = 0
    fix_rounds_agy = 0
    fix_rounds_claude = 0
    findings = {"critical": 0, "major": 0, "minor": 0}
    agy_tokens: int | None = None
    last_clean_approve = False
    had_non_approve = False

    for e in evs:
        kind = e.get("kind")
        d = e.get("data") if isinstance(e.get("data"), dict) else {}

        tok = d.get("agy_tokens")
        if isinstance(tok, int) and not isinstance(tok, bool) and tok >= 0:
            agy_tokens = tok

        if kind == "gate":
            passed = d.get("passed") is True
            if gate_first_pass is None:
                gate_first_pass = passed
            if not passed:
                gate_reds += 1
        elif kind == "fix-round":
            via = d.get("via")
            if via == "agy":
                fix_rounds_agy += 1
            elif via == "claude":
                fix_rounds_claude += 1
        elif kind == "review":
            raw_findings = d.get("findings")
            if isinstance(raw_findings, list):
                for f in raw_findings:
                    if isinstance(f, dict):
                        bucket = _SEV_MAP.get(str(f.get("severity") or "").lower())
                        if bucket:
                            findings[bucket] += 1
            if d.get("outcome") != "review-error":
                if _review_approved(d):
                    last_clean_approve = True
                else:
                    last_clean_approve = False
                    had_non_approve = True

    human_minutes = _human_minutes(evs)

    state = task.get("state")
    if state == "landed":
        outcome = "merged"
    elif state == "failed" or bool(task.get("archived_at")) or bool(task.get("archived")):
        outcome = "closed"
    else:
        outcome = "open"

    total_fixes = fix_rounds_agy + fix_rounds_claude
    is_needs_human = state == "needs_human" or _has_cap_stop(task, evs)
    if is_needs_human:
        result: str | None = "needs_human"
    elif last_clean_approve and total_fixes <= 1:
        result = "approved"
    elif total_fixes > 1 or last_clean_approve or total_fixes >= 1 or had_non_approve:
        result = "needed_fixes"
    elif state == "failed":
        result = "needs_human"
    else:
        result = None

    return {
        "lane": lane,
        "size": size,
        "plan_cost": plan_cost,
        "review_cost": review_cost,
        "claude_cost": claude_cost,
        "gate_first_pass": gate_first_pass,
        "gate_reds": gate_reds,
        "fix_rounds_agy": fix_rounds_agy,
        "fix_rounds_claude": fix_rounds_claude,
        "findings": findings,
        "human_minutes": human_minutes,
        "agy_tokens": agy_tokens,
        "outcome": outcome,
        "result": result,
    }


def _summarize_group(
    lane: str,
    size: str,
    items: list[tuple[float, int, dict]],
) -> dict:
    n = len(items)
    if n == 0:
        return {
            "lane": lane,
            "size": size,
            "tasks": 0,
            "first_pass_pct": None,
            "median_fix_rounds": None,
            "crit_major_per_task": None,
            "median_claude_usd": None,
            "median_human_min": None,
            "spark": [],
        }

    ordered = sorted(items, key=lambda x: (x[0], x[1]))
    recs = [r for _, _, r in ordered]

    gated = [r["gate_first_pass"] for r in recs if r["gate_first_pass"] is not None]
    first_pass_pct = (
        round(100.0 * sum(1 for g in gated if g) / len(gated), 1) if gated else None
    )
    median_fix_rounds = round(
        statistics.median([r["fix_rounds_agy"] + r["fix_rounds_claude"] for r in recs]), 4
    )
    crit_major = sum(r["findings"]["critical"] + r["findings"]["major"] for r in recs)
    crit_major_per_task = round(crit_major / n, 4)
    median_claude_usd = round(statistics.median([r["claude_cost"] for r in recs]), 4)
    median_human_min = round(statistics.median([r["human_minutes"] for r in recs]), 4)
    spark = [r["result"] for r in recs if r["result"] is not None][-SPARK_LIMIT:]

    return {
        "lane": lane,
        "size": size,
        "tasks": n,
        "first_pass_pct": first_pass_pct,
        "median_fix_rounds": median_fix_rounds,
        "crit_major_per_task": crit_major_per_task,
        "median_claude_usd": median_claude_usd,
        "median_human_min": median_human_min,
        "spark": spark,
    }


def graduation_hint(items: list[tuple[float, int, dict]]) -> dict:
    """Compute the agy+small graduation hint over the latest 5 finished (merged or closed)
    agy+small tasks ordered by created time.

    Fewer than 5 finished tasks returns `collecting`. Otherwise, counts how many of the
    latest 5 were approved with <= 1 total fix round; >= 4 returns `ready`, else `not_yet`.
    """
    finished = [
        item for item in items
        if item[2]["lane"] == "agy"
        and item[2]["size"] == "small"
        and item[2]["outcome"] in ("merged", "closed")
    ]
    n = len(finished)
    if n < GRADUATION_NEED:
        return {"state": "collecting", "n": n, "need": GRADUATION_NEED}

    finished.sort(key=lambda x: (x[0], x[1]))
    window = finished[-GRADUATION_NEED:]
    approved = sum(1 for _, _, r in window if r["result"] == "approved")
    if approved >= GRADUATION_PASS:
        return {
            "state": "ready",
            "approved": approved,
            "of": GRADUATION_NEED,
            "text": (
                f"small: {approved}/{GRADUATION_NEED} approved within 1 round, "
                "ready for medium"
            ),
        }
    return {
        "state": "not_yet",
        "approved": approved,
        "of": GRADUATION_NEED,
        "text": (
            f"small: {approved}/{GRADUATION_NEED} approved within 1 round, "
            "not ready for medium"
        ),
    }


def build(
    tasks: list[dict],
    events_by_task: dict[str, list[dict]] | list[list[dict]],
) -> dict:
    """Aggregate comparison groups per (lane, size), unsized count, and graduation hint.

    Graduation hint evaluates the latest 5 finished (`merged` or `closed`) agy+small tasks
    ordered by created time.
    """
    ev_lists = _events_for(tasks, events_by_task)
    all_items: list[tuple[float, int, dict]] = []
    buckets: dict[tuple[str, str], list[tuple[float, int, dict]]] = {
        (lane, size): [] for size in SIZES for lane in LANES
    }
    unsized = 0

    for idx, (task, evs) in enumerate(zip(tasks, ev_lists, strict=True)):
        rec = task_record(task, evs)
        created = _finite_num(task.get("created"))
        entry = (created if created is not None else 0.0, idx, rec)
        all_items.append(entry)
        if rec["size"] is None:
            unsized += 1
        else:
            buckets[(rec["lane"], rec["size"])].append(entry)

    groups = [
        _summarize_group(lane, size, buckets[(lane, size)])
        for size in SIZES
        for lane in LANES
    ]
    return {
        "groups": groups,
        "hint": graduation_hint(all_items),
        "unsized": unsized,
    }

"""Pure helpers: which model ran each pipeline step, and what it cost (from `result` events)."""
from __future__ import annotations

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

UNKNOWN = "unknown"
UNKNOWN_LABEL = "before model tracking"
UNKNOWN_TIP = "older result events have no model data"
LANES = ("claude", "agy")
# state in force when a result event lands -> phase
_PHASE = {"planning": "plan", "provisioning": "implement", "working": "implement", "fixing": "fix",
          "reviewing": "review"}
PHASES = ("plan", "implement", "fix", "review")
_NEW = re.compile(r"^claude-(opus|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?(?:\[.*\])?$")
_OLD = re.compile(r"^claude-(\d+)(?:-(\d{1,2}))?-(opus|sonnet|haiku)(?:-\d{8})?(?:\[.*\])?$")


def short_name(model: str | None) -> str:
    """'claude-sonnet-5-5' -> 'Sonnet 5.5'; unrecognised ids pass through unchanged."""
    if not model or not isinstance(model, str):
        return UNKNOWN
    m = _NEW.match(model)
    if m:
        fam, major, minor = m.group(1), m.group(2), m.group(3)
    else:
        o = _OLD.match(model)
        if not o:
            return model
        major, minor, fam = o.group(1), o.group(2), o.group(3)
    return f"{fam.capitalize()} {major}" + (f".{minor}" if minor else "")


def month_start(now: float, tz: str = "UTC") -> float:
    try:
        z = ZoneInfo(tz)
    except Exception:
        z = ZoneInfo("UTC")
    d = datetime.fromtimestamp(now, z)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def result_costs(data: dict) -> dict[str, float]:
    """model id -> cost for one result event. Old events (no model_usage) fall back to
    the event's total cost under its `model`, or 'unknown'."""
    mu = data.get("model_usage")
    if isinstance(mu, dict) and mu:
        out: dict[str, float] = {}
        for k, v in mu.items():
            c = _num(v.get("costUSD")) if isinstance(v, dict) else 0.0
            out[str(k)] = out.get(str(k), 0.0) + c
        return out
    model = data.get("model")
    return {model if isinstance(model, str) and model else UNKNOWN: _num(data.get("cost"))}


def phased_results(events: list[dict]):
    """Yield (phase, event) for each result event, phase from the preceding state event."""
    phase = "implement"
    for e in events:
        if e.get("kind") == "state":
            s = (e.get("data") or {}).get("state")
            if s in _PHASE:
                phase = _PHASE[s]
        elif e.get("kind") == "result" and isinstance(e.get("data"), dict):
            yield phase, e


def stage_models(events: list[dict]) -> dict[int, str]:
    """Last model used per board stage index (0 implement/fix, 2 review), display names."""
    out: dict[int, str] = {}
    for phase, e in phased_results(events):
        m = e["data"].get("model")
        if isinstance(m, str) and m:
            out[2 if phase == "review" else 0] = short_name(m)
    return out


def normalize_lane(lane: str | None) -> str:
    return "agy" if lane in ("agy", "agy-handoff") else "claude"


def _iter_lane_events(events_by_task: list, lanes: list[str | None] | None = None):
    """Yield (normalized_lane, events) pairs from either list[list[dict]] (+ optional lanes)
    or list[tuple[lane, list[dict]]]."""
    for idx, item in enumerate(events_by_task):
        if isinstance(item, tuple) and len(item) == 2:
            ln, evs = item
            yield normalize_lane(ln), evs
        else:
            ln = lanes[idx] if lanes is not None and idx < len(lanes) else None
            yield normalize_lane(ln), item


def _display_models(by_model: dict[str, float]) -> dict[str, float]:
    """Map the internal `unknown` key to UNKNOWN_LABEL for display."""
    out: dict[str, float] = {}
    for k, v in by_model.items():
        label = UNKNOWN_LABEL if k == UNKNOWN else k
        out[label] = out.get(label, 0.0) + v
    return out


def aggregate(events_by_task: list, since: float, *, lane: str | None = None,
              lanes: list[str | None] | None = None) -> dict:
    """Totals for result events at/after `since`: per model and per phase.
    When `lane` is given, only tasks in that lane are included."""
    want_lane = normalize_lane(lane) if lane is not None else None
    by_model: dict[str, float] = {}
    by_phase = {p: 0.0 for p in PHASES}
    total = 0.0
    for task_lane, evs in _iter_lane_events(events_by_task, lanes):
        if want_lane is not None and task_lane != want_lane:
            continue
        for phase, e in phased_results(evs):
            if (e.get("ts") or 0) < since:
                continue
            for m, c in result_costs(e["data"]).items():
                name = short_name(m)
                by_model[name] = by_model.get(name, 0.0) + c
                by_phase[phase] = by_phase.get(phase, 0.0) + c
                total += c
    return {"total": total, "by_model": by_model, "by_phase": by_phase}


def phase_spend(events_by_task: list, since: float = 0.0, *,
                lanes: list[str | None] | None = None) -> dict[str, dict]:
    """Per-lane phase totals (plan/implement/fix/review) for result events at/after `since`.
    Uses the exact same attribution as `aggregate` (all-time uses `since=0`)."""
    out: dict[str, dict] = {}
    for ln in LANES:
        agg = aggregate(events_by_task, since, lane=ln, lanes=lanes)
        out[ln] = {"total": agg["total"], "by_phase": agg["by_phase"]}
    return out


def month_spend(events_by_task: list, tz: str = "UTC",
                now: float | None = None, ledger_total: float | None = None,
                *, lane: str | None = None, lanes: list[str | None] | None = None) -> dict:
    """Month-to-date spend. With `ledger_total` (spend.month_to_date(), the cost_ledger
    sum — the authoritative total) the panel total IS that number. Any remainder the result
    events don't cover is 'unattributed' in both breakdowns; if events exceed the ledger the
    gap is clamped to 0 and reported as `events_exceed_ledger`."""
    now = time.time() if now is None else now
    out = aggregate(events_by_task, month_start(now, tz), lane=lane, lanes=lanes)
    out["by_model"] = _display_models(out["by_model"])
    out["unknown_tip"] = UNKNOWN_TIP
    if ledger_total is not None:
        gap = ledger_total - out["total"]
        if gap >= 0.005:
            out["by_model"]["unattributed"] = gap
            out["by_phase"]["unattributed"] = gap
        elif gap <= -0.005:
            out["events_exceed_ledger"] = -gap
        out["total"] = ledger_total
    return out

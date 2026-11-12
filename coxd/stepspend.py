"""Pure helpers: which model ran each pipeline step, and what it cost (from `result` events)."""
from __future__ import annotations

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

UNKNOWN = "unknown"
# state in force when a result event lands -> phase
_PHASE = {"provisioning": "implement", "working": "implement", "fixing": "fix",
          "reviewing": "review"}
PHASES = ("implement", "fix", "review")
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


def aggregate(events_by_task: list[list[dict]], since: float) -> dict:
    """Totals for result events at/after `since`: per model and per phase."""
    by_model: dict[str, float] = {}
    by_phase = {p: 0.0 for p in PHASES}
    total = 0.0
    for evs in events_by_task:
        for phase, e in phased_results(evs):
            if (e.get("ts") or 0) < since:
                continue
            for m, c in result_costs(e["data"]).items():
                name = short_name(m)
                by_model[name] = by_model.get(name, 0.0) + c
                by_phase[phase] += c
                total += c
    return {"total": total, "by_model": by_model, "by_phase": by_phase}


def month_spend(events_by_task: list[list[dict]], tz: str = "UTC", now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return aggregate(events_by_task, month_start(now, tz))

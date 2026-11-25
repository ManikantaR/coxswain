"""Pure agy token-usage validation, static price estimation, and monthly aggregation.

coxd never starts, signals, waits on, or parses agy. The human's `agy-next` script
posts a summary after publishing; this module validates that payload and estimates
its API-equivalent USD cost (for display only, paid by subscription — never added
to Claude spend or cost_ledger).
"""

from __future__ import annotations

import json
import math
import re
import time

import registry
import stepspend

TOKEN_KEYS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens")
ALL_KEYS = (*TOKEN_KEYS, "duration_seconds", "model")
MAX_TOKENS = 10**11
MAX_DURATION_S = 7 * 86400
_MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

# USD per 1M tokens. Output rate covers thinking tokens; cache_read is 0.10 * input.
PRICES: dict[str, dict[str, float]] = {
    "gemini-4-hydrogen-high": {"input": 1.50, "output": 7.50, "cache_read": 0.15},
    "gemini-4-hydrogen": {"input": 1.50, "output": 7.50, "cache_read": 0.15},
    "gemini-4-argon-high": {"input": 2.50, "output": 15.00, "cache_read": 0.25},
    "gemini-4-argon": {"input": 2.50, "output": 15.00, "cache_read": 0.25},
    "default": {"input": 1.50, "output": 7.50, "cache_read": 0.15},
}


def _valid_rate(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def load_prices() -> dict[str, dict[str, float]]:
    """Return price table with optional overrides from $COXD_HOME/agy-prices.json.
    Falls back to PRICES if the file is missing or malformed."""
    base = {k: dict(v) for k, v in PRICES.items()}
    path = registry.home() / "agy-prices.json"
    if not path.exists():
        return base
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return base
    if not isinstance(raw, dict):
        return base
    merged = dict(base)
    for model, entry in raw.items():
        if not isinstance(model, str) or not model or not isinstance(entry, dict):
            return base
        inp = entry.get("input")
        out = entry.get("output")
        if not (_valid_rate(inp) and _valid_rate(out)):
            return base
        inp_f = float(inp)  # type: ignore[arg-type]
        out_f = float(out)  # type: ignore[arg-type]
        if "cache_read" in entry:
            cr = entry.get("cache_read")
            if not _valid_rate(cr):
                return base
            cr_f = float(cr)  # type: ignore[arg-type]
        else:
            cr_f = 0.10 * inp_f
        merged[model] = {"input": inp_f, "output": out_f, "cache_read": cr_f}
    return merged


def clean(body: object) -> tuple[dict | None, str]:
    """Validate an agy-usage POST payload. Returns (cleaned_dict, error_message)."""
    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    extra = set(body.keys()) - set(ALL_KEYS)
    if extra:
        return None, f"unknown keys: {', '.join(sorted(str(k) for k in extra))}"
    for k in ALL_KEYS:
        if k not in body:
            return None, f"missing {k}"
    out: dict = {}
    for k in TOKEN_KEYS:
        v = body.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v <= MAX_TOKENS):
            return None, f"{k} must be an integer in 0..{MAX_TOKENS}"
        out[k] = v
    dur = body.get("duration_seconds")
    if (
        isinstance(dur, bool)
        or not isinstance(dur, (int, float))
        or not math.isfinite(dur)
        or not (0 <= dur <= MAX_DURATION_S)
    ):
        return None, f"duration_seconds must be a finite number in 0..{MAX_DURATION_S}"
    out["duration_seconds"] = dur
    model = body.get("model")
    if not isinstance(model, str) or not _MODEL_RE.match(model):
        return None, "model must match [A-Za-z0-9._-]{1,80}"
    out["model"] = model
    return out, ""


def _int_tok(v: object) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        return 0
    return v


def total_tokens(u: dict) -> int:
    return sum(_int_tok(u.get(k)) for k in TOKEN_KEYS)


def estimate(u: dict) -> float:
    """Estimated API price in USD:
    (input * in + (output + thinking) * out + cache_read * cache_read_rate) / 1e6."""
    if not isinstance(u, dict):
        return 0.0
    table = load_prices()
    model = u.get("model")
    rates = (
        (table.get(model) if isinstance(model, str) and model in table else None)
        or table.get("default")
        or PRICES["default"]
    )
    in_rate = float(rates["input"])
    out_rate = float(rates["output"])
    cache_rate = float(rates.get("cache_read", 0.10 * in_rate))
    inp = _int_tok(u.get("input_tokens"))
    out = _int_tok(u.get("output_tokens"))
    think = _int_tok(u.get("thinking_tokens"))
    cache = _int_tok(u.get("cache_read_tokens"))
    return (inp * in_rate + (out + think) * out_rate + cache * cache_rate) / 1_000_000.0


def latest(evs: list[dict]) -> dict | None:
    """Summary of the last `agy-usage` event of a task, or None if none exists."""
    for e in reversed(evs):
        if e.get("kind") == "agy-usage" and isinstance(e.get("data"), dict):
            d = e["data"]
            dur = d.get("duration_seconds")
            is_num = isinstance(dur, (int, float)) and not isinstance(dur, bool)
            dur_val = dur if is_num and math.isfinite(dur) else 0
            model = d.get("model")
            return {
                "tokens": total_tokens(d),
                "duration_seconds": dur_val,
                "model": model if isinstance(model, str) else "",
                "est_usd": estimate(d),
            }
    return None


def month(
    all_evs: list[list[dict]],
    tz: str = "UTC",
    now: float | None = None,
) -> dict:
    """Sum runs, tokens, and estimated USD across all `agy-usage` events in the current month."""
    now_ts = time.time() if now is None else now
    since = stepspend.month_start(now_ts, tz)
    runs = 0
    tokens = 0
    est_usd = 0.0
    for evs in all_evs:
        for e in evs:
            if e.get("kind") != "agy-usage":
                continue
            ts = e.get("ts")
            if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts < since:
                continue
            d = e.get("data")
            if not isinstance(d, dict):
                continue
            runs += 1
            tokens += total_tokens(d)
            est_usd += estimate(d)
    return {"runs": runs, "tokens": tokens, "est_usd": est_usd}

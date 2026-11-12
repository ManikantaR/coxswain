"""Per-role model + effort resolution: registry > explicit/env > shipped default.

Roles: worker, review. Env: COXD_WORKER_MODEL / COXD_WORKER_EFFORT /
COXD_REVIEW_MODEL / COXD_REVIEW_EFFORT. Registry keys (per repo, optional):
worker_model, worker_effort, review_model, review_effort. An invalid effort at any
level is ignored (falls through to the next level) rather than failing a task.
"""

from __future__ import annotations

import os

EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULTS = {
    "worker_model": "claude-sonnet-5-5",
    "worker_effort": "high",  # owner decision 2026-11-12
    "review_model": "claude-opus-5-5",
    "review_effort": "medium",
}


def _ok(key: str, v) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return None
    v = v.strip()
    if key.endswith("_effort") and v not in EFFORTS:
        return None
    return v


def resolve(reg: dict | None, **explicit: str | None) -> dict:
    """explicit: optional instance-level values (worker_model=..., etc.)."""
    reg = reg or {}
    out = {}
    for key, default in DEFAULTS.items():
        for cand in (reg.get(key), explicit.get(key),
                     os.environ.get("COXD_" + key.upper())):
            v = _ok(key, cand)
            if v:
                out[key] = v
                break
        else:
            out[key] = default
    return out

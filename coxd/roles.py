"""Per-role model + effort resolution: registry > explicit/env > shipped default.

Roles: worker, review. Env: COXD_WORKER_MODEL / COXD_WORKER_EFFORT /
COXD_REVIEW_MODEL / COXD_REVIEW_EFFORT. Registry keys (per repo, optional):
worker_model, worker_effort, review_model, review_effort. An invalid effort at any
level is ignored (falls through to the next level) rather than failing a task.
"""

from __future__ import annotations

import os
import sys

EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULTS = {
    "worker_model": "claude-sonnet-5-5",
    "worker_effort": "medium",
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
        for src, cand in (("registry", reg.get(key)), ("arg", explicit.get(key)),
                          ("env", os.environ.get("COXD_" + key.upper()))):
            v = _ok(key, cand)
            if v is None and cand not in (None, "") and key.endswith("_effort"):
                print(f"coxd: ignoring invalid {key} {cand!r} from {src}; "
                      f"expected one of {'|'.join(EFFORTS)}", file=sys.stderr)
            if v:
                out[key] = v
                break
        else:
            out[key] = default
    return out

"""Credentials that worker-controlled code must never see.

Workers (the LLM), the gate (runs the repo's test/lint/build — code the worker just
wrote) and provisioning (`pnpm install` runs package lifecycle scripts) all execute
worker-influenced code. Only the control plane (ship.py / landed.py inside the coxd
process) needs GitHub or Telegram credentials.
"""
from __future__ import annotations

import os

SECRET_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "COXD_TG_TOKEN")


def scrubbed_env(extra: dict | None = None) -> dict:
    """os.environ minus the secret vars (+ optional overrides) for a subprocess."""
    env = {k: v for k, v in os.environ.items() if k not in SECRET_VARS}
    env.update(extra or {})
    return env


def sdk_blanking() -> dict:
    """For the Agent SDK, which MERGES options.env over os.environ (it can't delete):
    blank every secret var so the spawned CLI and its tools inherit empty values."""
    return {k: "" for k in SECRET_VARS}

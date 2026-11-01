"""Credentials that worker-controlled code must never see.

Workers (the LLM), the gate (runs the repo's test/lint/build — code the worker just
wrote) and provisioning (`pnpm install` runs package lifecycle scripts) all execute
worker-influenced code. Only the control plane (ship.py / landed.py inside the coxd
process) needs GitHub or Telegram credentials.
"""
from __future__ import annotations

import os
import tempfile

SECRET_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "COXD_TG_TOKEN")


def scratch_home() -> str:
    """A throwaway COXD_HOME for worker-run code. The real one holds the live store,
    the repo registry and the digest markers; a worker running coxswain's own tests
    with the real COXD_HOME wrote 27 fixture tasks into the live store (2026-10-31)."""
    p = os.path.join(tempfile.gettempdir(), "coxd-worker-home")
    os.makedirs(p, exist_ok=True)
    return p


def scrubbed_env(extra: dict | None = None) -> dict:
    """os.environ minus the secret vars, with COXD_HOME pointed at a scratch dir
    (+ optional overrides) for a subprocess."""
    env = {k: v for k, v in os.environ.items() if k not in SECRET_VARS}
    env["COXD_HOME"] = scratch_home()
    env.update(extra or {})
    return env


def sdk_blanking() -> dict:
    """For the Agent SDK, which MERGES options.env over os.environ (it can't delete):
    blank every secret var so the spawned CLI and its tools inherit empty values, and
    point COXD_HOME away from the live store."""
    return {**{k: "" for k in SECRET_VARS}, "COXD_HOME": scratch_home()}

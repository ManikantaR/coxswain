"""Factory ON/OFF switch — per coxd instance (the NAS on, the laptop backup off).

OFF ("paused") means: start nothing new. Tasks already running finish normally,
dispatches still queue, and turning it back ON picks the queue up where it was.
The flag is a file in COXD_HOME so it survives restarts and rebuilds (the NAS
COXD_HOME is a volume) and is per-instance by construction.
"""
from __future__ import annotations

import json
import time

import registry


def _flag():
    return registry.home() / "paused"


def status() -> dict:
    p = _flag()
    if not p.exists():
        return {"paused": False}
    try:
        info = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        info = {}
    return {"paused": True, "since": info.get("since"), "by": info.get("by", "board")}


def is_paused() -> bool:
    return _flag().exists()


def set_paused(paused: bool, by: str = "board") -> dict:
    p = _flag()
    if paused:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"since": time.time(), "by": by}), encoding="utf-8")
    else:
        p.unlink(missing_ok=True)
    return status()

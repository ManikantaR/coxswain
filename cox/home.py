"""Coxswain home resolution + kill switch (T-01, DESIGN §2.9).

The home holds all runtime state (state/, data/, worktrees/). It is resolved
once and created on demand. Nothing here spends tokens or touches the network.
"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    """Resolve the coxswain home: env COX_HOME, else ~/cox-home."""
    raw = os.environ.get("COX_HOME")
    return Path(raw).expanduser() if raw else Path.home() / "cox-home"


def ensure_home() -> Path:
    """Resolve the home and make sure state/ and data/ exist."""
    root = home()
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    return root


def state_dir() -> Path:
    return home() / "state"


def data_dir() -> Path:
    return home() / "data"


def worktrees_dir() -> Path:
    return home() / "worktrees"


def paused_flag() -> Path:
    return state_dir() / "PAUSED"


def is_paused() -> bool:
    """True when dispatch/spawn/ship must halt (DESIGN §4.7)."""
    return paused_flag().exists()


def set_paused(paused: bool) -> None:
    ensure_home()
    flag = paused_flag()
    if paused:
        flag.touch()
    elif flag.exists():
        flag.unlink()

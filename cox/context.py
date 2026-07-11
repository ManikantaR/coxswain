"""Session context-fill estimate from a worker log (P5/D5).

Cherny/Karpathy: context is short-term memory that ROTS — quality degrades as a
session fills (~300–400k tokens). A fix round resumes the SAME session, so a
resume into an already-bloated session is where the rot bites. We estimate the
current fill from the worker log's latest input-token count (claude stream-json
or codex JSONL) so the board can show it (D5) and the fix round can warn (P5).
"""

from __future__ import annotations

import json
from pathlib import Path

ROT_LINE = 350_000  # ~ where context rot sets in; fill % is measured against this


def context_tokens(log_path: Path) -> int:
    """Latest input-token count (≈ current context size) in a worker log."""
    if not log_path.exists():
        return 0
    latest = 0
    for raw in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        usage = None
        if kind in ("result", "assistant"):
            usage = obj.get("usage") or (obj.get("message", {}) or {}).get("usage")
        elif kind == "turn.completed":
            usage = obj.get("usage")
        if isinstance(usage, dict):
            tin = (
                int(usage.get("input_tokens", 0))
                + int(usage.get("cache_read_input_tokens", 0))
                + int(usage.get("cached_input_tokens", 0))
            )
            latest = max(latest, tin)
    return latest


def fill_pct(tokens: int) -> int:
    return min(100, int(100 * tokens / ROT_LINE)) if tokens > 0 else 0

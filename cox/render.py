"""Render a worker's stream-json log into a compact narrated activity feed.

The orchestrator (and, later, the glance dashboard) must NOT read raw
stream-json — a single worker log is tens of KB of JSON per turn, which is
exactly what makes the control plane expensive and the human feel blind. This
turns that firehose into a short, human-scannable timeline:

    · <agent narration, one line>
    → Bash  git rebase origin/main
    → Edit  cox/gate.py
    ■ done  $1.61 · 3.4M in / 18k out

Pure stdlib, defensive (malformed lines skipped), reused by `cox peek` now and
the web dashboard's per-task feed later (see coxswain-observability-direction).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Which input field best labels each tool call, in priority order.
_ARG_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description")

_HOME = str(Path.home())
# Matches Unix absolute paths embedded in command strings.
_PATH_RE = re.compile(r"/[^\s'\"<>|&;()\[\]{}]+")


def _shorten_path(p: str) -> str:
    """Shorten a single matched absolute path token for compact display."""
    for marker in ("/cox-home/worktrees/", "/cox-home/data/"):
        idx = p.find(marker)
        if idx != -1:
            rest = p[idx + len(marker) :]
            parts = [s for s in rest.split("/") if s]
            tail = "/".join(parts[-2:]) if len(parts) >= 2 else rest
            return "…/" + tail
    if p.startswith(_HOME + "/"):
        return "~" + p[len(_HOME) :]
    if p == _HOME:
        return "~"
    return p


def _shorten_paths(text: str) -> str:
    """Replace long absolute paths in *text* with readable short forms."""
    return _PATH_RE.sub(lambda m: _shorten_path(m.group(0)), text)


def _tool_summary(name: str, inp: dict) -> str:
    for key in _ARG_KEYS:
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            one = " ".join(val.split())
            shortened = _shorten_paths(one)
            return f"{name}  {shortened[:80]}"
    return name


def _collapse_duplicates(lines: list[str]) -> list[str]:
    """Collapse consecutive identical lines into one with a count suffix."""
    if not lines:
        return lines
    result: list[str] = []
    prev = lines[0]
    count = 1
    for line in lines[1:]:
        if line == prev:
            count += 1
        else:
            result.append(prev if count == 1 else f"{prev}  (x{count})")
            prev = line
            count = 1
    result.append(prev if count == 1 else f"{prev}  (x{count})")
    return result


def _events(log_path: Path) -> list[str]:
    out: list[str] = []
    for raw in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "assistant":
            for c in obj.get("message", {}).get("content", []) or []:
                ctype = c.get("type")
                if ctype == "text":
                    txt = " ".join(str(c.get("text", "")).split())
                    if txt:
                        out.append(f"· {txt[:100]}")
                elif ctype == "tool_use":
                    name = str(c.get("name", "tool"))
                    out.append("→ " + _tool_summary(name, c.get("input") or {}))
        elif kind == "result":
            cost = obj.get("total_cost_usd")
            usage = obj.get("usage", {}) or {}
            tin = int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0))
            tout = int(usage.get("output_tokens", 0))
            verdict = "done" if not obj.get("is_error") else "error"
            cost_str = f"${cost:.2f}" if isinstance(cost, (int, float)) else "$?"
            out.append(f"■ {verdict}  {cost_str} · {tin} in / {tout} out")
    return _collapse_duplicates(out)


def summarize_stream(log_path: Path, n: int = 15) -> list[str]:
    """Return the last `n` narrated events from a stream-json worker log."""
    if not log_path.exists():
        return ["(no worker log yet)"]
    events = _events(log_path)
    if not events:
        return ["(no activity parsed — worker may still be starting; use --raw)"]
    return events[-n:]

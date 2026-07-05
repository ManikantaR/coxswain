"""Telegram notifier (T-16, M2). Placeholder until M0+M1 ship (ROADMAP P10).

One-way pings for AFK. Fired by the watcher only for wake verbs in the config
allowlist. Rate-limited to <=1 msg/task/10min. Network failure never crashes
the watcher — it logs and moves on.
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import home, store

RATE_LIMIT_SECS = 600


def _last_ping_path(task_id: str) -> Path:
    return store.task_state_dir(task_id) / "notify.ts"


def rate_limited(task_id: str, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    p = _last_ping_path(task_id)
    if not p.exists():
        return False
    try:
        last = float(p.read_text().strip())
    except ValueError:
        return False
    return (now - last) < RATE_LIMIT_SECS


def _record_ping(task_id: str) -> None:
    _last_ping_path(task_id).write_text(str(time.time()), encoding="utf-8")


def format_message(task_id: str, verb: str, detail: str) -> str:
    verbs_to_word = {
        "needs-decision": "your call",
        "review-findings": "approve / fix / reject",
        "pr-ready": "merge it",
        "ci-red": "your call",
    }
    reply = verbs_to_word.get(verb, "check in")
    return f"cox {task_id} — {verb}: {detail}\nReply in chat: {reply}"


def send(task_id: str, verb: str, detail: str, *, token: str, chat_id: str) -> bool:
    """Send one Telegram message. Returns False (never raises) on failure."""
    if rate_limited(task_id):
        return False
    text = format_message(task_id, verb, detail)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:  # noqa: S310
            ok = resp.status == 200
    except Exception:  # noqa: BLE001 - a notifier must never crash the watcher
        _log_failure(task_id, verb)
        return False
    if ok:
        _record_ping(task_id)
    return ok


def _log_failure(task_id: str, verb: str) -> None:
    home.ensure_home()
    (home.state_dir() / "notify.errors").open("a").write(f"{time.time()} {task_id} {verb}\n")

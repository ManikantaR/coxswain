"""Needs-you notifications (DESIGN-V35) — the AFK ping.

Fires when a task needs the captain: pr_ready (merge me) or needs_human (I'm
stuck). Zero-config default is ntfy (just a topic name — install the ntfy app,
subscribe, done); Telegram supported if a bot token+chat are configured. Config
from env (COXD_NTFY_TOPIC / COXD_NTFY_SERVER / COXD_TG_TOKEN / COXD_TG_CHAT /
COXD_BOARD_URL) or COXD_HOME/notify.json. Unconfigured = silent no-op. Sends
fire-and-forget on a daemon thread so the loop never blocks on the network.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import registry


def _config() -> dict:
    cfg: dict = {}
    p = registry.home() / "notify.json"
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cfg = {}
    env = {
        "ntfy_topic": "COXD_NTFY_TOPIC", "ntfy_server": "COXD_NTFY_SERVER",
        "telegram_token": "COXD_TG_TOKEN", "telegram_chat": "COXD_TG_CHAT",
        "board_url": "COXD_BOARD_URL",
    }
    for key, var in env.items():
        if os.environ.get(var):
            cfg[key] = os.environ[var]
    cfg.setdefault("ntfy_server", "https://ntfy.sh")
    return cfg


def notify(title: str, message: str, priority: str = "default") -> list[str]:
    """Send to every configured channel. Returns which fired (for tests/logs).

    Low-priority messages, and anything inside the household quiet window, arrive
    silently (no sound/vibration) — still delivered, just not waking anyone."""
    cfg = _config()
    url = cfg.get("board_url")
    silent = priority == "low" or in_quiet_hours()
    sent: list[str] = []
    if cfg.get("ntfy_topic"):
        _ntfy(cfg, title, message, url, "min" if silent else priority)
        sent.append("ntfy")
    if cfg.get("telegram_token") and cfg.get("telegram_chat"):
        _telegram(cfg, title, message, url, silent=silent)
        sent.append("telegram")
    return sent


def local_now() -> datetime:
    """Now in the household timezone (COXD_TZ, default America/New_York)."""
    return datetime.now(ZoneInfo(os.environ.get("COXD_TZ", "America/New_York")))


def in_quiet_hours(now: datetime | None = None) -> bool:
    """COXD_QUIET='HH:MM-HH:MM' (default 22:00-07:15), wrapping midnight."""
    now = now or local_now()
    spec = os.environ.get("COXD_QUIET", "22:00-07:15")
    try:
        a, b = (int(x[:2]) * 60 + int(x[3:5]) for x in spec.split("-"))
    except (ValueError, IndexError):
        return False
    m = now.hour * 60 + now.minute
    return (a <= m or m < b) if a > b else (a <= m < b)


def notify_async(title: str, message: str, priority: str = "default") -> None:
    """Fire-and-forget on a daemon thread (never blocks the event loop)."""
    def _run() -> None:
        try:
            notify(title, message, priority)
        except Exception:  # a failed notification must never break a task
            pass
    threading.Thread(target=_run, daemon=True).start()


def _ntfy(cfg: dict, title: str, message: str, url: str | None, priority: str) -> None:
    req = urllib.request.Request(
        f"{cfg['ntfy_server'].rstrip('/')}/{cfg['ntfy_topic']}", data=message.encode())
    req.add_header("Title", title)
    req.add_header("Priority", priority)
    if url:
        req.add_header("Click", url)
    urllib.request.urlopen(req, timeout=10)  # noqa: S310


def _telegram(cfg: dict, title: str, message: str, url: str | None,
              silent: bool = False) -> None:
    # Plain text: Markdown mode rejected messages containing task ids/states with
    # underscores (e.g. waiting_quota), and the failure was swallowed — a lost ping.
    text = f"{title}\n{message}" + (f"\n{url}" if url else "")
    data = urllib.parse.urlencode(
        {"chat_id": cfg["telegram_chat"], "text": text,
         "disable_notification": "true" if silent else "false"}).encode()
    urllib.request.urlopen(  # noqa: S310
        f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage",
        data=data, timeout=10)

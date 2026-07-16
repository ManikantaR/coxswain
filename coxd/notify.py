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
    """Send to every configured channel. Returns which fired (for tests/logs)."""
    cfg = _config()
    url = cfg.get("board_url")
    sent: list[str] = []
    if cfg.get("ntfy_topic"):
        _ntfy(cfg, title, message, url, priority)
        sent.append("ntfy")
    if cfg.get("telegram_token") and cfg.get("telegram_chat"):
        _telegram(cfg, title, message, url)
        sent.append("telegram")
    return sent


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


def _telegram(cfg: dict, title: str, message: str, url: str | None) -> None:
    text = f"*{title}*\n{message}" + (f"\n{url}" if url else "")
    data = urllib.parse.urlencode(
        {"chat_id": cfg["telegram_chat"], "text": text, "parse_mode": "Markdown"}).encode()
    urllib.request.urlopen(  # noqa: S310
        f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage",
        data=data, timeout=10)

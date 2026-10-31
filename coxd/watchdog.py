"""Stall watchdog + morning summary — the factory reports to the owner on its own.

- Stall: a task in an active state with no new event for STALL_MINUTES gets ONE
  Telegram ping ("quiet for 45 min"), recorded as a `stall-alert` event so it does
  not repeat until the task moves again.
- Morning summary: once a day at COXD_DIGEST_AT (default 07:20, COXD_TZ) — what is
  ready to merge, what needs you, what is running or waiting for the usage window,
  what landed, and what it cost in the last 24 h.
"""
from __future__ import annotations

import asyncio
import os
import time

import board
import notify
import registry
import store

STALL_MINUTES = int(os.environ.get("COXD_STALL_MINUTES", "45"))
CHECK_INTERVAL_S = 300
_ACTIVE = board._ACTIVE


def check_stalls(now: float | None = None) -> list[str]:
    """Ping once per stall; returns the task ids pinged."""
    now = now or time.time()
    pinged = []
    for t in store.list_tasks():
        if t["state"] not in _ACTIVE:
            continue
        evs = store.events(t["id"])
        if not evs or evs[-1]["kind"] == "stall-alert":
            continue
        quiet_min = (now - evs[-1]["ts"]) / 60
        if quiet_min < STALL_MINUTES:
            continue
        store.append_event(t["id"], "stall-alert", {"quiet_minutes": round(quiet_min)})
        notify.notify_async("coxd · task gone quiet",
                            f"{t['id']} ({t['repo']}) — no progress for {round(quiet_min)} min "
                            f"in '{t['state']}'. Check the board.")
        pinged.append(t["id"])
    return pinged


def summary_text(now: float | None = None) -> str:
    now = now or time.time()
    day_ago = now - 86400
    tasks = store.list_tasks()
    ready = [t for t in tasks if t["state"] == "pr_ready" and not t.get("archived_at")]
    stuck = [t for t in tasks if t["state"] == "needs_human" and not t.get("archived_at")]
    running = [t for t in tasks if t["state"] in _ACTIVE]
    waiting = [t for t in tasks if t["state"] == "waiting_quota"]
    landed = [t for t in tasks if t["state"] == "landed" and (t.get("updated") or 0) >= day_ago]
    cost = sum(t.get("cost") or 0 for t in tasks if (t.get("updated") or 0) >= day_ago)

    def line(t: dict, extra: str = "") -> str:
        return f"• {t['id']}" + (f" — {extra}" if extra else "")

    out = []
    if ready:
        out.append(f"Ready to merge ({len(ready)}):")
        out += [line(t, t.get("pr_url") or "") for t in ready]
    if stuck:
        out.append(f"Needs you ({len(stuck)}):")
        out += [line(t, t.get("reason") or "") for t in stuck]
    if running:
        out.append(f"Running: {', '.join(t['id'] for t in running)}")
    if waiting:
        when = max(t.get("resume_at") or 0 for t in waiting)
        out.append(f"Waiting for usage window ({len(waiting)}), resumes ~"
                   + time.strftime("%H:%M", time.localtime(when)))
    if landed:
        out.append(f"Landed in the last 24 h: {len(landed)}")
    if not out:
        out.append("Quiet night — nothing ran, nothing waiting.")
    out.append(f"Cost last 24 h: ${cost:.2f}")
    return "\n".join(out)


def maybe_send_morning_summary(now_local=None) -> bool:
    """Send once per local day, at/after COXD_DIGEST_AT. Marker file in COXD_HOME."""
    now_local = now_local or notify.local_now()
    hh, mm = (int(x) for x in os.environ.get("COXD_DIGEST_AT", "07:20").split(":"))
    if (now_local.hour, now_local.minute) < (hh, mm):
        return False
    marker = registry.home() / f"digest-{now_local:%Y-%m-%d}"
    if marker.exists():
        return False
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("sent")
    for old in registry.home().glob("digest-*"):  # keep the folder tidy
        if old != marker and old.stat().st_mtime < time.time() - 7 * 86400:
            old.unlink(missing_ok=True)
    notify.notify_async("coxd · morning summary", summary_text())
    return True


async def run() -> None:
    """Supervisor loop. A crashing pass must never take down serve."""
    while True:
        try:
            check_stalls()
            maybe_send_morning_summary()
        except Exception as e:  # noqa: BLE001
            print(f"watchdog pass failed: {e}")
        await asyncio.sleep(CHECK_INTERVAL_S)

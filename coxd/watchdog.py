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
import pii_health
import registry
import spend
import status
import store
import triage

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


def _previous_digest_ts(now: float) -> float:
    """When the last digest went out (newest marker mtime before today's), else 24 h ago."""
    try:
        ts = [p.stat().st_mtime for p in registry.home().glob("digest-*")]
    except OSError:
        ts = []
    ts = [x for x in ts if x < now]
    return max(ts) if ts else now - 86400


def summary_text(now: float | None = None, since: float | None = None) -> str:
    """Morning digest, grouped by repo. `since` = previous digest time (default 24 h)."""
    now = now or time.time()
    day_ago = now - 86400
    since = day_ago if since is None else since
    tasks = store.list_tasks()
    cost = sum(t.get("cost") or 0 for t in tasks if (t.get("updated") or 0) >= day_ago)

    def line(t: dict, extra: str = "") -> str:
        return f"• {t['id']}" + (f" — {extra}" if extra else "")

    repos = sorted({t["repo"] or "(no repo)" for t in tasks})
    out = []
    for repo in repos:
        rt = [t for t in tasks if (t["repo"] or "(no repo)") == repo]
        ready = [t for t in rt if t["state"] == "pr_ready" and not t.get("archived_at")]
        stuck = [t for t in rt if t["state"] == "needs_human" and not t.get("archived_at")]
        running = [t for t in rt if t["state"] in _ACTIVE]
        waiting = [t for t in rt if t["state"] == "waiting_quota"]
        landed = [t for t in rt if t["state"] == "landed" and (t.get("updated") or 0) >= day_ago]
        deploys, tiers = [], []
        for t in rt:
            for e in store.events(t["id"]):
                if e["ts"] < since:
                    continue
                if e["kind"] == "deployed":
                    deploys.append(e["ts"])
                elif e["kind"] == "greenlight":
                    tiers.append((e["ts"], t["id"], (e.get("data") or {}).get("tier")))
        sec = []
        if ready:
            sec.append(f"Ready to merge ({len(ready)}):")
            sec += [line(t, t.get("pr_url") or "") for t in ready]
        if stuck:
            sec.append(f"Needs you ({len(stuck)}):")
            sec += [line(t, f"{t.get('reason') or ''}. {triage.next_step_line(t)}"
                         if t.get("reason") else triage.next_step_line(t)) for t in stuck]
        if running:
            sec.append(f"Running: {', '.join(t['id'] for t in running)}")
        if waiting:
            when = max(t.get("resume_at") or 0 for t in waiting)
            sec.append(f"Waiting for usage window ({len(waiting)}), resumes ~"
                       + time.strftime("%H:%M", time.localtime(when)))
        if landed:
            sec.append(f"Landed in the last 24 h: {len(landed)}")
        if deploys:
            sec.append(f"Deploys since last digest: {len(deploys)}, latest "
                       + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(max(deploys))))
        if tiers:
            sec.append("Merge-level changes since last digest: "
                       + ", ".join(f"{i} → {tier or 'n/a'}" for _, i, tier in sorted(tiers)))
        if sec:
            out.append(f"== {repo} ==")
            out += sec
    if not out:
        out.append("Quiet night — nothing ran, nothing waiting.")
    pl = pii_health.summary_line(pii_health.status(now))
    if pl:
        out.append(pl)
    out.append(f"Cost last 24 h: ${cost:.2f}")
    out.append(f"Month to date: ${spend.month_to_date():.2f}")
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
    since = _previous_digest_ts(time.time())  # before this marker is written
    marker.write_text("sent")
    for old in registry.home().glob("digest-*"):  # keep the folder tidy
        if old != marker and old.stat().st_mtime < time.time() - 7 * 86400:
            old.unlink(missing_ok=True)
    notify.notify_async("coxd · morning summary", summary_text(since=since))
    return True


async def run() -> None:
    """Supervisor loop. A crashing pass must never take down serve."""
    while True:
        try:
            check_stalls()
            maybe_send_morning_summary()
            spend.maybe_alert()
            pii_health.maybe_ping(notify.local_now(), notify.notify_async)
            status.refresh_all()
        except Exception as e:  # noqa: BLE001
            print(f"watchdog pass failed: {e}")
        await asyncio.sleep(CHECK_INTERVAL_S)

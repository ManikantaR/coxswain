"""Wait out the subscription usage limit instead of failing the task.

When the 5-hour (or weekly) window runs out mid-task, the SDK sends a
RateLimitEvent with status 'rejected' and the exact `resets_at`. Before this, coxd
marked the task needs_human/worker-error and it sat until a human resumed it —
S1a lost 4h20m (2026-10-27) although coxd already knew the reset time.

Now the task is parked at `waiting_quota` with `resume_at` = reset + a small buffer.
The runner starts nothing else while the factory is quota-blocked, and when the time
comes the task is re-queued to continue the SAME SDK session (or retried fresh if it
had none yet) through the existing auto-resume path. Amends D10 ("rate limit →
needs-human") — see docs/DECISIONS.md D28.
"""
from __future__ import annotations

import random
import re
import time

import store

# resets_at + buffer (+ jitter) so we don't probe the instant the window flips.
RESUME_BUFFER_S = 90
# If the limit hit but no reset time came with it, try again after this long.
FALLBACK_WAIT_S = 30 * 60
QUOTA_RESUME = "auto-resume-after-usage-limit"


class QuotaWait(Exception):
    """Raised by the loop when a stage was cut off by the usage limit."""

    def __init__(self, reset_at: float, stage: str):
        super().__init__(f"usage limit during {stage}; resets at {reset_at:.0f}")
        self.reset_at = reset_at
        self.stage = stage


def rejected_reset_at(info) -> float | None:
    """The reset epoch from a RateLimitInfo whose status is 'rejected', else None."""
    if info is None or getattr(info, "status", None) != "rejected":
        return None
    ts = getattr(info, "resets_at", None)
    return float(ts) if ts else time.time() + FALLBACK_WAIT_S


def reset_from_result(result) -> float | None:
    """Fallback detection from the final ResultMessage: a 429, or the CLI's
    'usage limit reached|<epoch>' text."""
    if result is None:
        return None
    text = str(getattr(result, "result", "") or "")
    if getattr(result, "api_error_status", None) == 429 or "usage limit" in text.lower():
        m = re.search(r"\|(\d{10})\b", text)
        return float(m.group(1)) if m else time.time() + FALLBACK_WAIT_S
    return None


def park(task_id: str, reset_at: float, stage: str) -> float:
    """Move a task to waiting_quota; returns the resume time."""
    resume_at = reset_at + RESUME_BUFFER_S + random.uniform(0, 60)  # noqa: S311
    store.set_resume_at(task_id, resume_at)
    store.set_state(task_id, "waiting_quota", f"usage limit ({stage})")
    store.append_event(task_id, "waiting-quota", {"stage": stage, "reset_at": reset_at,
                                                  "resume_at": resume_at})
    import notify
    t = store.get_task(task_id) or {}
    when = time.strftime("%H:%M", time.localtime(resume_at))
    notify.notify_async("coxd · paused for usage limit",
                        f"{task_id} ({t.get('repo', '')}) — resumes ~{when}, no action needed",
                        priority="low")
    return resume_at


def blocked_until(now: float | None = None) -> float | None:
    """Latest future resume_at among waiting tasks: while set, start nothing new
    (any new task would hit the same exhausted window)."""
    now = now or time.time()
    future = [t["resume_at"] for t in store.waiting_quota_tasks()
              if t.get("resume_at") and t["resume_at"] > now]
    return max(future) if future else None


def release_due(auto_retry_reason: str, now: float | None = None) -> list[str]:
    """Re-queue every waiting task whose time has come. With a session it continues
    that session (QUOTA_RESUME); without one it is a clean fresh retry."""
    now = now or time.time()
    released = []
    for t in store.waiting_quota_tasks():
        if t.get("resume_at") and t["resume_at"] > now:
            continue
        reason = QUOTA_RESUME if t.get("session_id") else auto_retry_reason
        store.set_resume_at(t["id"], None)
        store.set_state(t["id"], "queued", reason)
        store.append_event(t["id"], "info", {"note": "usage window reset — resuming"})
        released.append(t["id"])
    return released

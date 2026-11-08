"""needs_human triage: reason -> {retry, next_step}, plus the one-shot auto-retry rules.

Only transient failures (worker-error, review-error) are retried, once, ~10 min
after entering needs_human. Everything else needs a person; the alert says what to do.
"""
from __future__ import annotations

import store

RETRY_DELAY_S = 600
AUTO_RETRY_EVENT = "auto-retry"
AUTO_RETRY_NOTE = ("Automatic retry (once): the previous attempt hit a transient error. "
                   "Run `git status` and `git log` to see what is already done, then "
                   "continue the task. Do not push.")
RETRY_TEXT = "retrying once automatically in ~10 min"

_GENERIC = {
    "retry": False,
    "next_step": (
        "Open the task on the board, read the latest events, "
        "then resume with a note or archive it."
    ),
}

TRIAGE: dict[str, dict] = {
    "worker-error": {
        "retry": True,
        "next_step": (
            "The model run errored; if the automatic retry fails, resume it "
            "with a note from the board."
        ),
    },
    "review-error": {
        "retry": True,
        "next_step": (
            "The reviewer call crashed; if the automatic retry fails, "
            "resume it from the board to re-run the review."
        ),
    },
    "gate-red": {
        "retry": False,
        "next_step": (
            "Read the failing gate step on the board, then resume with a "
            "note on how to fix it."
        ),
    },
    "review-findings": {
        "retry": False,
        "next_step": (
            "Read the review findings on the board, then resume with a note "
            "on which to fix."
        ),
    },
    "push-rejected": {
        "retry": False,
        "next_step": (
            "Check the branch and remote permissions, fix the push problem, "
            "then resume the task."
        ),
    },
    "pr-error": {
        "retry": False,
        "next_step": (
            "Check that gh is authenticated and the branch is pushed, then "
            "resume to open the PR."
        ),
    },
    "pii-block": {
        "retry": False,
        "next_step": (
            "Check the flagged rule and location on the board; fix the text "
            "or add an allow entry."
        ),
    },
    "pii-scan-error": {
        "retry": False,
        "next_step": (
            "The PII scan itself failed; check the scanner setup, then "
            "resume the task."
        ),
    },
    "coxd-error": {
        "retry": False,
        "next_step": (
            "coxd hit an internal error; check the task's error event, then "
            "resume or retry it."
        ),
    },
    "orphaned-on-restart": {
        "retry": False,
        "next_step": (
            "coxd restarted with a PR already open; check the PR, then "
            "merge it, close it, or resume."
        ),
    },
    "stopped-by-user": {
        "retry": False,
        "next_step": (
            "You stopped this task; resume with a note when you want it to "
            "continue."
        ),
    },
}


def triage(reason: str | None) -> dict:
    return TRIAGE.get(reason or "", _GENERIC)


def retry_pending(task: dict, events: list[dict] | None = None) -> bool:
    """True if this task is eligible for (and has not yet used) its one auto-retry."""
    if task.get("state") != "needs_human" or task.get("pr_url"):
        return False
    if not triage(task.get("reason"))["retry"]:
        return False
    evs = store.events(task["id"]) if events is None else events
    return not any(e["kind"] == AUTO_RETRY_EVENT for e in evs)


def next_step_line(task: dict) -> str:
    """The plain-words line appended to alerts/digests for a needs_human task."""
    if retry_pending(task):
        return f"Next: {RETRY_TEXT}."
    return triage(task.get("reason"))["next_step"]


def due(task: dict, now: float, events: list[dict] | None = None) -> bool:
    if not retry_pending(task, events):
        return False
    evs = store.events(task["id"]) if events is None else events
    # measure from when it entered needs_human, not `updated` (other setters bump that)
    entered = max((e["ts"] for e in evs if e["kind"] == "state"),
                  default=task.get("updated") or now)
    return now - entered >= RETRY_DELAY_S

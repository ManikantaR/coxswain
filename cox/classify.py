"""Deterministic status-line classification (T-08, DESIGN §2.2).

Zero tokens. A worker appends sparse status lines to data/<id>/status.log; the
watcher calls classify() on each new line. A line is *actionable* iff its verb
is in ACTIONABLE_VERBS — everything else (working:, heartbeats) is absorbed
silently so an idle fleet never wakes the orchestrator (firstmate's insight).
"""

from __future__ import annotations

from dataclasses import dataclass

# Verbs that must wake the orchestrator. Kept in sync with DESIGN §2.2 and the
# Telegram allowlist (M2). Derived states (stale, worker-exited) are synthesized
# by the watcher, not written by workers, but share this vocabulary.
ACTIONABLE_VERBS: frozenset[str] = frozenset(
    {
        "done",
        "failed",
        "blocked",
        "needs-decision",
        "gate-verdict",
        "pr-ready",
        "ci-green",
        "ci-red",
        "stale",
        "worker-exited",
    }
)


@dataclass(frozen=True)
class Wake:
    verb: str
    detail: str


def classify(line: str) -> Wake | None:
    """Return a Wake if the line is actionable, else None.

    Status-line grammar: "<verb>: <free text>" (or a bare "<verb>"). Leading/
    trailing whitespace and a "PROGRESS <ts>" liveness prefix are ignored.
    """
    text = line.strip()
    if not text:
        return None
    verb, _, detail = text.partition(":")
    verb = verb.strip().lower()
    if verb in ACTIONABLE_VERBS:
        return Wake(verb=verb, detail=detail.strip())
    return None

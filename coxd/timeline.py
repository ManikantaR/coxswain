"""Per-task timing derived from recorded `state` events. Pure: no I/O."""
from __future__ import annotations

_BUCKET = {
    "queued": "queued", "provisioning": "provision", "working": "implement",
    "gating": "gate", "fixing": "fix", "reviewing": "review", "shipping": "ship",
    "ci-watching": "ci", "waiting_quota": "paused", "needs_human": "human",
    "pr_ready": "human",
}
_WORKING = ("provision", "implement", "gate", "fix", "review", "ship")
_START_STATES = {"provisioning", "working", "gating", "fixing", "reviewing",
                 "shipping", "ci-watching"}
_FINISH_STATES = {"pr_ready", "needs_human", "landed"}
_IDLE_STATES = _FINISH_STATES | {"failed"}


def _state_of(ev: dict) -> str | None:
    d = ev.get("data")
    return d.get("state") if isinstance(d, dict) else None


def derive(task: dict, events: list, now: float) -> dict:
    states = [(float(e["ts"]), _state_of(e)) for e in events
              if e.get("kind") == "state" and _state_of(e) and e.get("ts") is not None]
    states.sort(key=lambda x: x[0])
    created = task.get("created")
    cur = task.get("state")

    buckets = {b: 0.0 for b in ("queued", "provision", "implement", "gate", "fix",
                                "review", "ship", "ci", "paused", "human")}
    for (ts, st), (nxt, _) in zip(states, states[1:], strict=False):
        b = _BUCKET.get(st or "")
        if b:
            buckets[b] += max(0.0, nxt - ts)

    started = next((ts for ts, st in states if st in _START_STATES), created)
    if started is None and states:
        started = states[0][0]

    finished = None
    landed = None
    merge_wait = 0.0
    if cur == "landed":
        landed_i = next((i for i in range(len(states) - 1, -1, -1)
                         if states[i][1] == "landed"), None)
        if landed_i is not None:
            landed = states[landed_i][0]
            # Factory work ends at the last hand-off to a human before the merge;
            # fall back to the landed timestamp when there was none.
            fin_i = next((i for i in range(landed_i, -1, -1)
                          if states[i][1] in ("pr_ready", "needs_human")), None)
            finished = states[fin_i][0] if fin_i is not None else landed
            merge_wait = max(0.0, landed - finished)
            # The final hand-off -> landed segment is merge wait, not human wait.
            if fin_i is not None and fin_i + 1 == landed_i:
                buckets["human"] -= max(0.0, landed - finished)
    elif cur in _FINISH_STATES:
        finished = next((ts for ts, st in reversed(states) if st in _FINISH_STATES), None)

    in_progress = cur not in _IDLE_STATES
    wall = 0.0
    if started is not None:
        if finished is not None:
            end = finished
        elif in_progress:
            end = now
        else:
            end = states[-1][0] if states else started
        wall = max(0.0, end - started)

    gate_fixes = review_rounds = 0
    for e in events:
        d = e.get("data") if isinstance(e.get("data"), dict) else {}
        if e.get("kind") == "fix-trigger":
            if d.get("source") == "gate":
                gate_fixes += 1
        elif e.get("kind") == "review" and d.get("pass") == "re-review":
            review_rounds += 1
    attempts = sum(1 for _, st in states if st == "queued")

    # The open (last) segment is excluded from buckets above, so a running task's
    # current-state time is added here to keep active_s growing with `now`.
    if states and in_progress:
        b = _BUCKET.get(states[-1][1] or "")
        if b and b != "human":
            buckets[b] += max(0.0, now - states[-1][0])

    return {
        "started_at": started, "finished_at": finished, "landed_at": landed,
        "in_progress": in_progress, "wall_s": wall,
        "active_s": sum(buckets[b] for b in _WORKING),
        "paused_quota_s": buckets["paused"], "human_wait_s": buckets["human"],
        "ci_s": buckets["ci"], "merge_wait_s": merge_wait, "attempts": attempts,
        "gate_fixes": gate_fixes, "review_rounds": review_rounds,
    }

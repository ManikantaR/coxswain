"""Per-repo STATUS.md written by coxd into COXD_HOME/status/<repo>.md.

One short file the lead session reads instead of polling the board. It lives in
COXD_HOME only — nothing is ever committed to the target repo. Refreshed by
store.append_event on state / deploy / streak events (best-effort, never raises).
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import registry
import store

_SAFE = re.compile(r"[^A-Za-z0-9._-]")

# Next step for a stuck task, keyed by reason prefix; fallback below.
_NEXT = (
    ("orphaned-on-restart", "PR may exist: check it, then Resume or archive on the board"),
    ("stopped-by-user", "Resume from the board if still wanted, else archive"),
    ("coxd-error", "Read the task events; Resume once the cause is fixed"),
)


def _fname(repo: str) -> str:
    n = _SAFE.sub("_", repo or "").strip(".")
    return n or "_"


def status_path(repo: str) -> Path:
    return registry.home() / "status" / f"{_fname(repo)}.md"


def _next_step(t: dict) -> str:
    reason = t.get("reason") or ""
    for pre, step in _NEXT:
        if reason.startswith(pre):
            return step
    if t["state"] == "failed":
        return "Read the task events; re-dispatch with a sharper brief"
    return "Read the task events, then Resume with guidance or archive"


def _line(t: dict) -> str:
    return " ".join((t.get("intent") or t.get("brief") or "").split())[:80]


def _last_deploy(repo: str, tasks: list[dict]) -> str:
    ts = None
    for t in tasks:
        for e in store.events(t["id"]):
            if e["kind"] == "deployed" and (ts is None or e["ts"] > ts):
                ts = e["ts"]
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else "never"


def render(repo: str) -> str:
    tasks = [t for t in store.list_tasks()
             if t["repo"] == repo and not store.is_archived(t)]
    open_ = [t for t in tasks if t["state"] in ("queued", "running", "waiting_quota")]
    ready = [t for t in tasks if t["state"] == "pr_ready"]
    stuck = [t for t in tasks if t["state"] in ("needs_human", "failed")]
    tier = next((t["greenlight_tier"] for t in tasks if t.get("greenlight_tier")), None)
    out = [f"# {repo} — coxd status", ""]
    out.append(f"## Open ({len(open_)})")
    out += [f"- {t['id']} [{t['state']}] {_line(t)}" for t in open_] or ["- none"]
    out.append(f"## Ready to merge ({len(ready)})")
    out += [f"- {t['id']} {t.get('pr_url') or '(no PR url)'} "
            f"tier={t.get('greenlight_tier') or 'n/a'}" for t in ready] or ["- none"]
    out.append(f"## Stuck ({len(stuck)})")
    out += [f"- {t['id']} [{t['state']}: {t.get('reason') or '-'}] next: {_next_step(t)}"
            for t in stuck] or ["- none"]
    out += ["", f"Last deploy: {_last_deploy(repo, tasks)}",
            f"Merge level: {tier or 'n/a'} (latest greenlight tier)",
            f"Clean streak: {store.get_clean_streak(repo)}", ""]
    return "\n".join(out)


def write(repo: str) -> Path:
    p = status_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(render(repo))
    os.replace(tmp, p)
    return p


def refresh_for_task(task_id: str) -> None:
    """Best-effort hook: status is a convenience and must never break a state change."""
    try:
        t = store.get_task(task_id)
        if t and t.get("repo"):
            write(t["repo"])
    except Exception:  # noqa: BLE001
        pass

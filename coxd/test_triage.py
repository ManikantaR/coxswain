"""Triage + one-shot auto-retry tests. Run: COXD_HOME=$(mktemp -d) python test_triage.py"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-test-")

import factory  # noqa: E402
import loop  # noqa: E402
import notify  # noqa: E402
import store  # noqa: E402
import supervisor  # noqa: E402
import triage  # noqa: E402
import watchdog  # noqa: E402

KNOWN = ["worker-error", "review-error", "gate-red", "review-findings", "push-rejected",
         "pr-error", "pii-block", "pii-scan-error", "coxd-error", "orphaned-on-restart",
         "stopped-by-user"]


def _mk(tid, reason, *, pr=None, session="s1"):
    store.create_task(tid, "acme-widgets", "brief", "/wt")
    if session:
        store.set_session(tid, session)
    if pr:
        store.set_pr_url(tid, pr)
    store.set_state(tid, "needs_human", reason)
    return store.get_task(tid)


def _scan(later=700):
    calls = []

    async def fake_resume(tid, wm, rm, eff, note=None, after_quota=False):
        calls.append((tid, note))

    supervisor._run_one_resume = fake_resume

    async def go():
        started = supervisor._auto_retry_scan("w", "r", "medium", now=time.time() + later)
        await asyncio.gather(*supervisor._running.values())
        supervisor._running.clear()
        return started
    return asyncio.run(go()), calls


def run():
    for r in KNOWN:
        assert triage.triage(r)["next_step"], r
        assert triage.triage(r)["retry"] == (r in ("worker-error", "review-error")), r
    u = triage.triage("something-new")
    assert u["retry"] is False and u["next_step"]
    assert triage.triage(None)["retry"] is False

    # worker-error, no PR -> exactly one resume; second worker-error -> none
    _mk("acme-1", "worker-error")
    assert _scan(300)[0] == []                       # too early
    started, calls = _scan()
    assert started == ["acme-1"] and len(calls) == 1 and "Automatic retry" in calls[0][1]
    assert any(e["kind"] == "auto-retry" for e in store.events("acme-1"))
    store.set_state("acme-1", "needs_human", "worker-error")
    assert _scan()[0] == []

    _mk("acme-pr", "worker-error", pr="https://example.invalid/pr/1")
    assert _scan()[0] == []
    _mk("acme-stop", "stopped-by-user")
    assert _scan()[0] == []
    _mk("acme-gate", "gate-red")
    assert _scan()[0] == []

    # paused -> deferred, not dropped
    _mk("acme-2", "review-error")
    factory.set_paused(True)
    assert _scan()[0] == []
    assert not any(e["kind"] == "auto-retry" for e in store.events("acme-2"))
    factory.set_paused(False)
    assert _scan()[0] == ["acme-2"]

    # alert text: fake notifier, no network
    sent = []
    notify.notify_async = lambda title, msg, priority="default": sent.append((title, msg))
    store.create_task("acme-3", "acme-widgets", "b", "/wt")
    loop._terminal("acme-3", "needs_human", "pii-block")
    assert triage.triage("pii-block")["next_step"] in sent[-1][1]
    store.create_task("acme-4", "acme-widgets", "b", "/wt")
    loop._terminal("acme-4", "needs_human", "worker-error")
    assert "retrying once automatically in ~10 min" in sent[-1][1]

    digest = watchdog.summary_text()
    assert triage.triage("pii-block")["next_step"] in digest
    assert "retrying once automatically" in digest or "acme-4" in digest
    print("ok")


if __name__ == "__main__":
    run()

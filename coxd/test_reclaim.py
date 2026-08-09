"""Unit test for supervisor._reclaim_orphans — the crash-auto-resume decision.

coxd has no pytest harness (tests/ is the retired `cox` package), so this is a
standalone script. Run against a THROWAWAY home so it never touches the live store:

    cd coxd && COXD_HOME=$(mktemp -d) .venv/bin/python test_reclaim.py

Verifies that on restart an orphaned task is routed by how far it got:
  - open PR            -> needs_human (never risk a duplicate PR)
  - live SDK session   -> queued + AUTO_RESUME (runner resumes the session)
  - no session yet     -> queued + AUTO_RETRY  (safe fresh re-run)
  - not in an active state -> left untouched.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("COXD_HOME", tempfile.mkdtemp(prefix="coxd-test-"))

import store  # noqa: E402
import supervisor  # noqa: E402
from supervisor import _AUTO_RESUME, _AUTO_RETRY  # noqa: E402


def _mk(tid: str, state: str, *, session: str | None = None, pr: str | None = None) -> None:
    store.create_task(tid, "repo", "brief", "/wt")
    if session:
        store.set_session(tid, session)
    if pr:
        store.set_pr_url(tid, pr)
    store.set_state(tid, state, None)


def run() -> None:
    _mk("t-pr", "ci-watching", session="s1", pr="http://pr/1")  # PR open
    _mk("t-sess", "working", session="s2")                       # session, no PR
    _mk("t-fresh", "working")                                    # no session
    _mk("t-review", "reviewing", session="s3")                   # active, session
    _mk("t-shipping", "shipping")                                # active, no session
    _mk("t-landed", "landed", session="s4")                      # not active
    _mk("t-queued", "queued")                                    # not active
    _mk("t-nh", "needs_human")                                   # not active

    supervisor._reclaim_orphans()

    expect = {
        "t-pr": ("needs_human", "orphaned-on-restart"),
        "t-sess": ("queued", _AUTO_RESUME),
        "t-fresh": ("queued", _AUTO_RETRY),
        "t-review": ("queued", _AUTO_RESUME),
        "t-shipping": ("queued", _AUTO_RETRY),
        "t-landed": ("landed", None),     # None reason = "unchanged, don't assert reason"
        "t-queued": ("queued", None),
        "t-nh": ("needs_human", None),
    }

    ok = True
    for tid, (exp_state, exp_reason) in expect.items():
        t = store.get_task(tid)
        got = (t["state"], t["reason"])
        passed = got[0] == exp_state and (exp_reason is None or got[1] == exp_reason)
        ok = ok and passed
        print(f"{'PASS' if passed else 'FAIL'} {tid}: got {got} exp ({exp_state}, {exp_reason})")

    print("\nALL PASS ✓" if ok else "\nSOME FAILED ✗")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    run()

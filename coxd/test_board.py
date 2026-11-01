"""Regression test: /api/tasks payload must build (it crashed after the deploy-cache
refactor left a stale `__defaults__[0].clear()` call).

Standalone (coxd has no pytest harness). Throwaway home:
    cd coxd && COXD_HOME=$(mktemp -d) .venv/bin/python test_board.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("COXD_HOME", tempfile.mkdtemp(prefix="coxd-board-"))

import board  # noqa: E402
import store  # noqa: E402

store.create_task("t1", "aura-tutor", "brief", "/wt", repo_path="/repo/aura-tutor")
store.set_state("t1", "landed", "merged")

payload = board._tasks_payload()
assert any(r["id"] == "t1" for r in payload["tasks"]), payload
assert all(isinstance(r["timing"], dict) for r in payload["tasks"]), payload
assert payload["tz"]
assert board._tasks_payload() == payload  # second pass uses the TTL cache, no crash
print("ok test_board")

os.environ["COXD_HOST_LABEL"] = "mac"
fp = board._factory_payload()
assert fp["host"] == "mac" and "role" in fp and "paused" in fp, fp
del os.environ["COXD_HOST_LABEL"]
assert board._factory_payload()["host"], "default host falls back to hostname"
assert board._factory_payload()["role"] == "primary"
assert "offline-panel" in board._HTML and "coxd is not running on this machine" in board._HTML
assert "id=foff" in board._HTML and "FACTORY OFF" in board._HTML
assert "coxd-v2" in open(board.__file__).read()
assert "function timingLine" in board._HTML and "data-run=" in board._HTML
assert "waiting_quota:['b-warn'" in board._HTML
assert "t.timing" in board._HTML.split("const k=JSON.stringify")[1][:400]
print("ok test_board factory/offline")

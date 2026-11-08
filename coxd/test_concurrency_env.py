"""COXD_CONCURRENCY sets the serve cap; --concurrency overrides; board shows it.
Run: cd coxd && ../.venv/bin/python test_concurrency_env.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-conc-")

import board  # noqa: E402
import cli  # noqa: E402
import factory  # noqa: E402
import supervisor  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


seen: list[int] = []


def fake_serve(**kw):
    seen.append(kw["concurrency"])

    async def _noop():
        return None
    return _noop()


supervisor.serve = fake_serve


def run(argv, env):
    os.environ.pop("COXD_CONCURRENCY", None)
    if env is not None:
        os.environ["COXD_CONCURRENCY"] = env
    seen.clear()
    cli.main(argv)
    return seen[-1]


check("default is 1", run(["serve"], None) == 1)
check("env sets cap", run(["serve"], "4") == 4)
check("flag overrides env", run(["serve", "--concurrency", "3"], "4") == 3)
try:
    run(["serve"], "zero")
    check("bad env rejected", False)
except SystemExit:
    check("bad env rejected", True)

factory.set_concurrency(5)
c = TestClient(board.app, base_url="http://localhost:8791")
check("/api/factory shows cap", c.get("/api/factory").json()["concurrency"] == 5)
check("tasks payload shows cap", board._tasks_payload()["factory"]["concurrency"] == 5)
print("FAILS:", fails)
sys.exit(1 if fails else 0)

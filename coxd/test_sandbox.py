"""Standalone test for sandbox.py (run worker code as an unprivileged user).

    cd coxd && ../.venv/bin/python test_sandbox.py

The real uid switch needs root + setpriv (the NAS image); here we check the command
shapes, the no-op default, and that worktree git runs without credentials.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-sbx-")
os.environ.pop("COXD_WORKER_USER", None)
os.environ["GH_TOKEN"] = "ghp_fake_for_test"

import sandbox  # noqa: E402

fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


# unset → laptop behaviour: nothing changes
check("wrap is a no-op without a worker user", sandbox.wrap(["sh", "-c", "x"]) == ["sh", "-c", "x"])
check("cli_path is None without a worker user", sandbox.cli_path() is None)
sandbox.own(Path("/definitely/not/here"))  # must not raise or chown anything
check("own is a no-op without a worker user", True)

# worktree git runs without the GitHub token and with the scratch COXD_HOME
probe = ["sh", "-c", 'echo "${GH_TOKEN:-none} $COXD_HOME"']
r = sandbox.run_in_worktree(probe, tempfile.gettempdir())
tok, home = r.stdout.split()
check("run_in_worktree hides GH_TOKEN", tok == "none")
check("run_in_worktree hides the live COXD_HOME", home != os.environ["COXD_HOME"])

# set → setpriv prefix that drops uid/gid/groups/caps and fixes HOME
os.environ["COXD_WORKER_USER"] = "coxw"
w = sandbox.wrap(["sh", "-c", "x"])
check("wrap switches uid and gid", "--reuid=coxw" in w and "--regid=coxw" in w)
check("wrap resets groups, no new privs, no caps",
      "--init-groups" in w and "--no-new-privs" in w and "--inh-caps=-all" in w)
check("wrap sets the worker HOME", "HOME=/home/coxw" in w)
check("wrap keeps the command last", w[0] == "setpriv" and w[-3:] == ["sh", "-c", "x"])
check("cli_path points at the wrapper", sandbox.cli_path() == sandbox.CLI_WRAPPER)
wrapper = Path(__file__).resolve().parent.parent / "deploy" / "coxd-claude"
check("the wrapper uses the same setpriv flags",
      all(f in wrapper.read_text() for f in ("--init-groups", "--no-new-privs", "--inh-caps=-all")))

print("OK" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)

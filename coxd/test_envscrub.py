"""Worker-controlled code never sees GitHub/Telegram credentials.

Standalone (coxd has no pytest harness):
    cd coxd && COXD_HOME=$(mktemp -d) ../.venv/bin/python test_envscrub.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("COXD_HOME", tempfile.mkdtemp(prefix="coxd-env-"))
os.environ["GH_TOKEN"] = "ghp_fake_for_test"
os.environ["COXD_TG_TOKEN"] = "123:fake"
os.environ["KEEP_ME"] = "1"

import envscrub  # noqa: E402
import worktree  # noqa: E402

fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


env = envscrub.scrubbed_env({"EXTRA": "x"})
check("scrubbed_env drops secrets", "GH_TOKEN" not in env and "COXD_TG_TOKEN" not in env)
check("scrubbed_env keeps the rest + extras", env.get("KEEP_ME") == "1" and env.get("EXTRA") == "x")
blank = envscrub.sdk_blanking()
check("sdk_blanking blanks every secret", all(blank[k] == "" for k in envscrub.SECRET_VARS))

# provision: a lifecycle script that tries to read the token sees nothing
wt = __import__("pathlib").Path(tempfile.mkdtemp(prefix="coxd-wt-"))
(wt / "package.json").write_text("{}")
orig = worktree.subprocess.run
seen: dict = {}


def spy(*a, **k):
    seen.update(k.get("env") or {})
    return orig(["true"], capture_output=True, text=True)


worktree.subprocess.run = spy
worktree.provision(wt)
worktree.subprocess.run = orig
check("provision runs without GH_TOKEN", "GH_TOKEN" not in seen and seen.get("KEEP_ME") == "1")

print("OK" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)

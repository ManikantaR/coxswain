"""Tests for greenlight tier storage + per-repo clean-PR streak.

    cd coxd && .venv/bin/python test_streak.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-streak-")
os.environ["COXD_GIT_EMAIL"] = "bot-ci@example.invalid"

import landed  # noqa: E402
import store  # noqa: E402

res = []


def check(name, cond):
    res.append(cond)
    print(("PASS" if cond else "FAIL"), name)


BOT = "bot-ci@example.invalid"
BOTS = [landed.coxd_identity_email(None)]
check("identity from COXD_GIT_EMAIL", BOTS == [BOT])
REPO = "acme-widgets"
# (author_email, committer_email); a Co-Authored-By trailer never appears here.
OWN = [(BOT, BOT), (BOT, BOT)]
HUMAN = [(BOT, BOT), ("bob@example.invalid", "bob@example.invalid")]

store.create_task("t1", REPO, "brief", "/tmp/wt")
store.set_greenlight_tier("t1", "GREEN")
check("tier stored", store.get_task("t1")["greenlight_tier"] == "GREEN")
check("streak starts 0", store.get_clean_streak(REPO) == 0)

check("coxd-identity commits (claude co-author ignored) are clean",
      landed.is_clean_merge("GREEN", True, OWN, BOTS))
for i in (1, 2):
    store.create_task(f"c{i}", REPO, "b", "/tmp/wt")
    store.record_merge(REPO, landed.is_clean_merge("GREEN", True, OWN, BOTS), task_id=f"c{i}")
check("streak up to 2", store.get_clean_streak(REPO) == 2)
check("other repo independent", store.get_clean_streak("zeta-gizmos") == 0)

store.record_merge(REPO, True, task_id="c2")  # second cleanup pass, same task
check("repeat pass for same task is a no-op", store.get_clean_streak(REPO) == 2)
check("only one streak event", sum(e["kind"] == "streak" for e in store.events("c2")) == 1)

check("committer-only mismatch not clean",
      not landed.is_clean_merge("GREEN", True, [(BOT, "bob@example.invalid")], BOTS))
check("AMBER without reasons not clean", not landed.is_clean_merge("AMBER", True, OWN, BOTS))
SIZE = ["large diff (9 files, +300/-10)"]
check("AMBER size-only is clean", landed.is_clean_merge("AMBER", True, OWN, BOTS, SIZE))
check("AMBER with medium finding not clean",
      not landed.is_clean_merge("AMBER", True, OWN, BOTS, ["1 medium finding(s)"] + SIZE))
check("RED not clean", not landed.is_clean_merge("RED", True, OWN, BOTS, SIZE))
check("GREEN still clean", landed.is_clean_merge("GREEN", True, OWN, BOTS, []))
check("red CI not clean", not landed.is_clean_merge("GREEN", False, OWN, BOTS))
check("no commits not clean", not landed.is_clean_merge("GREEN", True, [], BOTS))
check("no identity not clean", not landed.is_clean_merge("GREEN", True, OWN, [""]))
check("no tier not clean", not landed.is_clean_merge(None, True, OWN, BOTS))
check("extra allowlist honored",
      landed.is_clean_merge("GREEN", True, [("ci2@example.invalid",) * 2],
                            BOTS + ["ci2@example.invalid"]))

store.create_task("c3", REPO, "b", "/tmp/wt")
store.record_merge(REPO, landed.is_clean_merge("GREEN", True, HUMAN, BOTS), task_id="c3")
check("commit by another email resets to 0", store.get_clean_streak(REPO) == 0)
store.create_task("c4", REPO, "b", "/tmp/wt")
store.record_merge(REPO, True, task_id="c4")
check("streak restarts at 1", store.get_clean_streak(REPO) == 1)


# --- tier reasons stored with the task
store.create_task("r1", REPO, "b", "/tmp/wt")
store.set_greenlight_tier("r1", "AMBER", SIZE)
check("reasons stored", json.loads(store.get_task("r1")["greenlight_reasons"]) == SIZE)

# --- merge levels: streak 10 -> proposed + exactly one notification
import registry  # noqa: E402

pings = []
landed.notify.notify_async = lambda *a, **k: pings.append(a)
R2 = "orbit-gadgets"
registry.save(R2, {"test": "t"})
check("default level manual", registry.merge_level(R2) == "manual")
for i in range(9):
    store.create_task(f"m{i}", R2, "b", "/tmp/wt")
    store.record_merge(R2, True, task_id=f"m{i}")
    landed._maybe_propose_auto(R2)
check("streak 9 -> still manual, no ping", registry.merge_level(R2) == "manual" and not pings)
for i in (9, 10, 11):
    store.create_task(f"m{i}", R2, "b", "/tmp/wt")
    store.record_merge(R2, True, task_id=f"m{i}")
    landed._maybe_propose_auto(R2)
check("streak 10 -> proposed", registry.merge_level(R2) == "proposed")
check("exactly one notification (not repeated)", len(pings) == 1)

# --- nothing but approve sets auto
check("propose never sets auto", registry.propose_auto_merge(R2) is False
      and registry.merge_level(R2) == "proposed")
check("approve on unknown repo refused", registry.approve_auto_merge("no-such-repo") is False)
registry.save("plain-repo", {"test": "t"})
check("approve without proposal refused", not registry.approve_auto_merge("plain-repo")
      and registry.merge_level("plain-repo") == "manual")
for i in range(12):
    store.create_task(f"p{i}", "plain-repo", "b", "/tmp/wt")
    store.record_merge("plain-repo", True, task_id=f"p{i}")
    landed._maybe_propose_auto("plain-repo")
check("long streak alone yields proposed, not auto", registry.merge_level("plain-repo") == "proposed")
check("approve sets auto", registry.approve_auto_merge(R2) and registry.merge_level(R2) == "auto")
store.create_task("m99", R2, "b", "/tmp/wt")
store.record_merge(R2, False, task_id="m99")
landed._maybe_propose_auto(R2)
check("non-clean merge / re-propose leaves auto untouched", registry.merge_level(R2) == "auto")

sys.exit(0 if all(res) else 1)

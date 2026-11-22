"""PII gate: numeric rules (card/ssn) skipped for review_exclude paths; denylist/secret never.
Run: python coxd/test_pii_vendor_skip.py  (invented values only)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-pvs-home-")
tempfile.tempdir = tempfile.mkdtemp(prefix="coxd-pvs-tmp-")

import pii  # noqa: E402
import registry  # noqa: E402
import ship  # noqa: E402
import store  # noqa: E402

fails = 0
TERM = "zzterm-test-1"
Path(pii.denylist_path()).write_text(TERM + "\n")
DIGITS = "4929" + "0000" + "0000" + "0000"  # invented, Luhn-valid


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


tmp = Path(tempfile.mkdtemp(prefix="coxd-pvs-git-"))
GENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.test",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.test"}


def g(*a: str) -> None:
    subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True, env=GENV)


g("init", "-q", "-b", "main")
(tmp / "a.txt").write_text("base\n")
g("add", ".")
g("commit", "-qm", "base")
g("update-ref", "refs/remotes/origin/main", "HEAD")


def branch(name: str, files: dict[str, str]) -> None:
    g("checkout", "-q", "-b", name, "origin/main")
    for p, txt in files.items():
        (tmp / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp / p).write_text(txt + "\n")
    g("add", ".")
    g("commit", "-qm", "work")


branch("vend", {"vendor/x.min.js": f"var n={DIGITS};"})
branch("src", {"src/app.py": f"n = {DIGITS}"})
branch("vdeny", {"vendor/x.min.js": f"var a='{TERM}';"})
branch("gen", {"generated/a.js": f"var n={DIGITS};"})
branch("none", {"src/ok.py": "x = 1"})
branch("both", {"vendor/x.min.js": f"var n={DIGITS};", "src/app.py": f"n = {DIGITS}"})
branch("named", {f"vendor/{TERM}.min.js": f"var n={DIGITS};"})

store.create_task("tp", "pvsrepo", "brief", "/wt", repo_path=str(tmp))
ship._pr_title = lambda t: "t"
ship._greenlight = lambda t, wt: ("", "", [])
ship._build_body = lambda t, wt, h="", **kw: "b"


def gate(br: str, **reg) -> tuple[str | None, list[dict]]:
    registry.save("pvsrepo", {"target_branch": "main", **reg})
    before = len(store.events("tp"))
    out = ship._pii_gate("tp", store.get_task("tp"), tmp, br, tmp)
    return out, [e for e in store.events("tp")[before:]]


def payload(e: dict) -> dict:
    d = e.get("data", e.get("payload", {}))
    return json.loads(d) if isinstance(d, str) else d


def scan_ev(evs: list[dict]) -> list[dict]:
    return [payload(e) for e in evs if (e.get("kind") or e.get("type")) == "pii-scan"]


out, evs = gate("vend")
sc = scan_ev(evs)
check("vendor/x.min.js with digits passes", out is None)
check("pii-scan event lists the path only",
      sc and sc[0]["numeric_skipped_paths"] == ["vendor/x.min.js"] and DIGITS not in json.dumps(evs))
out, evs = gate("src")
check("same digits in normal source blocks (card)", out == "pii-block"
      and '"card"' in json.dumps(evs) and not scan_ev(evs))
out, evs = gate("vdeny")
check("denylist term in vendored file still blocks", out == "pii-block"
      and '"denylist"' in json.dumps(evs))
out, _ = gate("gen", review_exclude=["generated/**"])
check("registry review_exclude override applies", out is None)
out, _ = gate("vend", review_exclude=["generated/**"])
check("override REPLACES defaults (vendor now blocks)", out == "pii-block")
out, _ = gate("vend", review_exclude="vendor")
check("malformed review_exclude falls back to defaults", out is None)
out, evs = gate("none")
check("no skipped paths -> no pii-scan event", out is None and not scan_ev(evs))
out, evs = gate("both")
check("mixed: normal-source digits still block", out == "pii-block")
out, evs = gate("named")
check("path naming a denylist term blocks and is never named in events",
      out == "pii-block" and TERM not in json.dumps(evs))

print("FAILED" if fails else "all ok")
sys.exit(1 if fails else 0)

"""PII denylist health (invented data only).

    cd coxd && python test_pii_health.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HOME = Path(tempfile.mkdtemp(prefix="coxd-piih-"))
os.environ["COXD_HOME"] = str(HOME)

import intake  # noqa: E402
import pii  # noqa: E402
import pii_health  # noqa: E402
import registry  # noqa: E402
import ship  # noqa: E402
import store  # noqa: E402
import watchdog  # noqa: E402

fails = 0
NOW = datetime(2026, 11, 8, 8, 0).timestamp()
TERM = "Amy Example"


def check(name, cond):
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


def setup(deny="present", age=None, cands=None):
    for p in (pii.denylist_path(), HOME / "pii" / "last-reviewed",
              HOME / "pii" / "new-candidates.count", HOME / "pii-ping-last"):
        p.unlink(missing_ok=True)
    (HOME / "pii").mkdir(exist_ok=True)
    if deny == "present":
        pii.denylist_path().write_text(TERM + "\n")
    elif deny == "empty":
        pii.denylist_path().write_text("")
    if age is not None:
        (HOME / "pii" / "last-reviewed").write_text(str(int(NOW - age * 86400)))
    if cands is not None:
        (HOME / "pii" / "new-candidates.count").write_text(str(cands))
    return pii_health.status(NOW)


check("ok", setup(age=12, cands=0)["level"] == "ok")
s = setup(age=12, cands=0)
check("age 12 / no candidates", s["age_days"] == 12 and s["new_candidates"] == 0 and s["exists"] and not s["empty"])
check("due by age", setup(age=34, cands=0)["level"] == "due")
check("due by candidates", setup(age=5, cands=2)["level"] == "due")
check("overdue", setup(age=46, cands=0)["level"] == "overdue")
check("never reviewed -> overdue", setup(age=None)["level"] == "overdue")
check("missing -> blocked", setup(deny="missing", age=1, cands=0)["level"] == "blocked")
s = setup(deny="empty", age=1, cands=0)
check("empty -> blocked", s["level"] == "blocked" and s["empty"] and s["exists"])

# counts only
setup(age=34, cands=5)
line = pii_health.summary_line(pii_health.status(NOW))
check("digest line text", line == "PII denylist: reviewed 34 days ago, 5 new candidates — run the review")
sent = []
pii_health.maybe_ping(datetime(2026, 11, 1, 9), lambda t, m: sent.append(t + m))
blob = "\n".join([line, watchdog.summary_text(NOW), repr(pii_health.pill()),
                  pii_health.ping_text(pii_health.status(NOW))] + sent)
check("ping sent", len(sent) == 1)
check("no denylist term in output", TERM not in blob and "Example" not in blob)
check("digest includes line", "PII denylist:" in watchdog.summary_text(NOW))
setup(age=3, cands=0)
check("digest silent when ok", "PII denylist" not in watchdog.summary_text(NOW))

# ping schedule
D = lambda d, h=9: datetime(2026, 12, d, h)  # noqa: E731
setup(age=40, cands=1)
got = []
send = lambda t, m: got.append(1)  # noqa: E731
check("1st pings", pii_health.maybe_ping(D(1), send))
check("1st again same day: no", not pii_health.maybe_ping(D(1, 18), send))
check("day 2: no", not pii_health.maybe_ping(D(2), send))
check("day 3: no", not pii_health.maybe_ping(D(3), send))
check("day 4: yes (3 days)", pii_health.maybe_ping(D(4), send))
check("day 5: no", not pii_health.maybe_ping(D(5), send))
check("day 7: yes", pii_health.maybe_ping(D(7), send))
# review lands: ok -> stops
(HOME / "pii" / "last-reviewed").write_text(str(int(D(8).timestamp())))
(HOME / "pii" / "new-candidates.count").write_text("0")
check("after review: no ping day 10", not pii_health.maybe_ping(D(10), send))
check("after review: no ping day 13", not pii_health.maybe_ping(D(13), send))
(HOME / "pii-ping-last").unlink()
check("monthly 1st still pings when ok",
      pii_health.maybe_ping(datetime(2027, 1, 1, 9), send))

# fail closed
registry_entries = {}
registry.load = lambda n: registry_entries.get(n)  # type: ignore
intake.enabled_repos = lambda: [{"name": "widgets", "path": "/nonexistent"}]  # type: ignore
processed = []
intake._process_repo = lambda n, p: processed.append(n) or 1  # type: ignore
intake._reconcile_stuck = lambda n, p: None  # type: ignore
setup(deny="missing", age=1, cands=0)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    r = intake.poll_once()
check("blocked: intake takes nothing", r == 0 and not processed)
check("blocked: reason recorded once", buf.getvalue().count("PII denylist missing") == 1
      and intake.status().get("blocked") == "pii-denylist-missing")
setup(age=1, cands=0)
with contextlib.redirect_stdout(io.StringIO()):
    r = intake.poll_once()
check("ok: intake proceeds", r == 1 and processed == ["widgets"] and "blocked" not in intake.status())

# ship gate: drive _pii_gate; entry lookup happens after title/body, so stub those
ship._pr_title = lambda t: "t"  # type: ignore
ship._greenlight = lambda t, wt: ("t", "h", [])  # type: ignore
ship._build_body = lambda t, wt, h: "b"  # type: ignore
ship._git_text = lambda *a, **k: "abc123"  # type: ignore
ship._resolve_base = lambda *a, **k: "main"  # type: ignore
ship._pii_items = lambda *a, **k: {}  # type: ignore
ship._added_paths = lambda *a, **k: []  # type: ignore
store.append_event = lambda *a, **k: None  # type: ignore
gate = lambda: ship._pii_gate("t1", {"repo": "r"}, Path("."), "b", Path("."))  # noqa: E731
for deny in ("missing", "empty"):
    setup(deny=deny)
    registry_entries["r"] = {"intake": True}
    check(f"{deny}: intake repo blocked", gate() == "pii-scan-error")
    registry_entries["r"] = {"data_globs": ["**/*.pdf"], "fixture_dir": "fx/"}
    check(f"{deny}: data_globs repo blocked", gate() == "pii-scan-error")
    registry_entries["r"] = {}
    check(f"{deny}: plain repo unchanged", gate() is None)
setup(age=1, cands=0)
registry_entries["r"] = {"intake": True}
check("present: intake repo passes", gate() is None)
setup(deny="present", age=60, cands=0)
check("overdue does not block", gate() is None)

print("FAILS:", fails)
sys.exit(1 if fails else 0)

"""Per-repo data_globs guard: new data files outside fixture_dir block; inside allowed.
Run: python coxd/test_data_globs.py  (invented repo/paths only)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-dg-home-")

import pii  # noqa: E402
import registry  # noqa: E402
import ship  # noqa: E402

fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)


G = ["**/*.pdf", "**/*.jpg", "inbox/**"]
FX = "tests/fixtures/synthetic/"
blocked = lambda paths: bool(pii.data_file_hits(paths, G, FX))  # noqa: E731

check("pdf outside fixture dir blocked", blocked(["docs/scan.pdf"]))
check("top-level pdf blocked", blocked(["scan.pdf"]))
check("uppercase ext blocked", blocked(["a/SCAN.PDF"]))
check("pdf inside fixture dir allowed", not blocked(["tests/fixtures/synthetic/a.pdf"]))
check("lookalike dir not exempt", blocked(["tests/fixtures/synthetic-real/a.pdf"]))
check("inbox/** blocked", blocked(["inbox/x.txt"]))
check("non-matching file allowed", not blocked(["src/app.py"]))
check("no globs = off", not pii.data_file_hits(["a.pdf"], [], None))
check("no fixture_dir = nothing exempt", bool(pii.data_file_hits(["t/a.pdf"], G, None)))
check("hit never names the path", all("pdf" not in h.location
                                      for h in pii.data_file_hits(["x.pdf"], G, FX)))

# end to end through git: added paths on a branch
tmp = Path(tempfile.mkdtemp(prefix="coxd-dg-git-"))
GENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.test",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.test"}


def g(*a: str) -> None:
    subprocess.run(["git", *a], cwd=tmp, check=True, capture_output=True, env=GENV)


g("init", "-q", "-b", "main")
(tmp / "a.txt").write_text("base\n")
g("add", ".")
g("commit", "-qm", "base")
g("update-ref", "refs/remotes/origin/main", "HEAD")
g("checkout", "-qb", "b1")
(tmp / "tests/fixtures/synthetic").mkdir(parents=True)
(tmp / "tests/fixtures/synthetic/ok.pdf").write_text("fake\n")
g("add", ".")
g("commit", "-qm", "fixture")
paths = ship._added_paths(tmp, "origin/main", "refs/heads/b1")
check("fixture pdf allowed via git", not pii.data_file_hits(paths, G, FX))
(tmp / "bad.pdf").write_text("fake\n")
g("add", ".")
g("commit", "-qm", "bad")
g("rm", "-q", "bad.pdf")
g("commit", "-qm", "rm")  # added then removed: still caught
paths = ship._added_paths(tmp, "origin/main", "refs/heads/b1")
check("pdf outside fixture blocked via git (even if removed later)",
      bool(pii.data_file_hits(paths, G, FX)))

# a tracked file renamed into inbox/ is an add of a data path
g("checkout", "-q", "-b", "b2", "origin/main")
(tmp / "inbox").mkdir()
g("mv", "a.txt", "inbox/a.txt")
g("commit", "-qm", "rename into inbox")
paths = ship._added_paths(tmp, "origin/main", "refs/heads/b2")
check("rename into inbox/ flagged", bool(pii.data_file_hits(paths, G, FX)))

# malformed data_globs fails closed through the gate (no per-character globs)
import store  # noqa: E402

g("branch", "clean", "origin/main")
store.create_task("tdg", "dgrepo", "brief", "/wt", repo_path=str(tmp))
ship._pr_title = lambda t: "t"
ship._greenlight = lambda t, wt: ("", "", [])
ship._build_body = lambda t, wt, h="": "b"
ship._pii_items = lambda *a, **k: {}
for bad in ("*.pdf", 5, ["ok", 3], {"a": 1}):
    registry.save("dgrepo", {"data_globs": bad, "fixture_dir": FX, "target_branch": "main"})
    out = ship._pii_gate("tdg", store.get_task("tdg"), tmp, "b1", tmp)
    check(f"bad data_globs {bad!r} -> pii-scan-error", out == "pii-scan-error")
registry.save("dgrepo", {"data_globs": G, "fixture_dir": FX, "target_branch": "main"})
check("valid config, clean branch passes gate",
      ship._pii_gate("tdg", store.get_task("tdg"), tmp, "clean", tmp) is None)
check("valid config, data file added -> pii-block",
      ship._pii_gate("tdg", store.get_task("tdg"), tmp, "b1", tmp) == "pii-block")
registry.save("dgrepo", {"data_globs": G, "fixture_dir": 7, "target_branch": "main"})
check("bad fixture_dir -> pii-scan-error",
      ship._pii_gate("tdg", store.get_task("tdg"), tmp, "b1", tmp) == "pii-scan-error")

# brief
guarded = registry.data_guard_brief({"data_globs": G, "fixture_dir": FX}, "do x")
check("brief carries guard when set", "synthetic fixtures only" in guarded.lower())
check("brief untouched when unset", registry.data_guard_brief({}, "do x") == "do x")

sys.exit(1 if fails else 0)

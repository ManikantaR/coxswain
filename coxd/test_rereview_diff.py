"""gate.rereview_input: delta + previous findings + full --stat; safe fallbacks."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-rrd-")
os.environ.pop("COXD_WORKER_USER", None)  # run git as the current user
tempfile.tempdir = tempfile.mkdtemp(prefix="coxd-rrd-tmp-")  # private scratch_home()

import gate  # noqa: E402

wt = Path(tempfile.mkdtemp(prefix="coxd-rrd-wt-"))


def git(*a):
    return subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@e.x",
                           *a], check=True, capture_output=True, text=True).stdout.strip()


git("init", "-q", "-b", "main")
(wt / "base.txt").write_text("base\n")
git("add", ".")
git("commit", "-qm", "base")
git("update-ref", "refs/remotes/origin/main", "HEAD")
git("checkout", "-qb", "feat")
(wt / "feature.py").write_text("FEATURE_MARKER_LINE = 1\n")
git("add", ".")
git("commit", "-qm", "feature")
reviewed = gate.head_sha(wt)
assert reviewed and reviewed == git("rev-parse", "HEAD")
(wt / "fix.py").write_text("FIX_MARKER_LINE = 2\n")
git("add", ".")
git("commit", "-qm", "fix")
assert gate.head_sha(wt) != reviewed

findings = [{"severity": "high", "file": "feature.py", "message": "PREV_FINDING_TEXT"}]
out = gate.rereview_input(wt, {}, reviewed, findings)
assert "FIX_MARKER_LINE" in out, out
assert "PREV_FINDING_TEXT" in out
assert "feature.py" in out and "fix.py" in out  # --stat covers the full diff
assert "FEATURE_MARKER_LINE" not in out  # feature hunk body is not re-sent
assert "RE-REVIEW" in out and "MUST be re-reported" in out

# no new commits: explicit empty delta, still a valid input
out = gate.rereview_input(wt, {}, gate.head_sha(wt), findings)
assert "no changes since the last review" in out

# bogus / unknown / non-hex / option-looking since_sha -> full diff fallback
for bad in (None, "", "deadbeef" * 5, "--output=x", "zzzzzzz"):
    out = gate.rereview_input(wt, {}, bad, findings)
    assert "FEATURE_MARKER_LINE" in out and "FIX_MARKER_LINE" in out, bad
    assert "RE-REVIEW" not in out

# not an ancestor (e.g. after a rebase): fallback to the full diff
git("checkout", "-q", "-b", "other", "main")
(wt / "o.txt").write_text("o\n")
git("add", ".")
git("commit", "-qm", "o")
other = gate.head_sha(wt)
git("checkout", "-q", "feat")
out = gate.rereview_input(wt, {}, other, findings)
assert "FEATURE_MARKER_LINE" in out and "RE-REVIEW" not in out

# head_sha on a non-repo -> None
assert gate.head_sha(Path(tempfile.mkdtemp())) is None
print("ok test_rereview_diff")

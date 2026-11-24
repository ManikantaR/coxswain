"""review_diff: vendored/generated files are summarised not embedded; oversized diffs are capped.

Standalone: cd coxd && COXD_HOME=$(mktemp -d) python test_review_diff.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-rd-")
os.environ.pop("COXD_WORKER_USER", None)  # run git as the current user
tempfile.tempdir = tempfile.mkdtemp(prefix="coxd-rd-tmp-")  # private scratch_home()

import gate  # noqa: E402


def git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd,
                   check=True, capture_output=True)


def write(root: Path, rel: str, data, mode="w"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, mode) as f:
        f.write(data)


def make_repo(big_source: bool = False) -> Path:
    root = Path(tempfile.mkdtemp(prefix="coxd-rd-repo-"))
    git(root, "init", "-q", "-b", "main")
    write(root, "README.md", "hi\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    git(root, "checkout", "-q", "-b", "feat")
    write(root, "vendor/x.min.js", "var MINIFIEDMARKER=1;" * 100_000)  # ~2 MB
    write(root, "web/vendor/y.js", "VENDOREDDEEP\n")
    write(root, "web/package-lock.json", "{\"lock\": \"LOCKMARKER\"}\n")
    write(root, "static/app.min.css", "a{b:c}" * 10)
    write(root, "static/blob.bin", b"\x00\x01\x02" * 50, "wb")
    write(root, "src/a.py", "print('SRCMARKER')\n")
    if big_source:
        write(root, "src/big1.py", "x = 1  # BIG1\n" * 40_000)
        write(root, "src/big2.py", "y = 2  # BIG2\n" * 40_000)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "feat")
    return root


def main() -> None:
    root = make_repo()
    out = gate.review_diff(root, {})
    assert "SRCMARKER" in out and "src/a.py" in out, out[:500]
    assert "MINIFIEDMARKER" not in out and "LOCKMARKER" not in out
    assert "VENDOREDDEEP" not in out
    assert len(out) < 5_000, len(out)
    assert "Not shown" in out
    for p in ("vendor/x.min.js", "web/vendor/y.js", "web/package-lock.json",
              "static/app.min.css", "static/blob.bin"):
        assert p in out.split("diff --git")[0], p
    assert "+1/-0 lines" in out and "binary, 150 bytes" in out, out[:600]
    # gate.diff() itself is unchanged (full diff)
    assert "MINIFIEDMARKER" in gate.diff(root)

    # None / missing key / malformed → defaults
    assert out == gate.review_diff(root, {"review_exclude": None})
    assert out == gate.review_diff(root, {"review_exclude": "*.py"})

    # registry override REPLACES the defaults
    o = gate.review_diff(root, {"review_exclude": ["*.py"]})
    assert "SRCMARKER" not in o and "src/a.py" in o.split("diff --git")[0]
    assert "MINIFIEDMARKER" in o  # vendor no longer excluded (and not budget-capped here)
    o = gate.review_diff(root, {"review_exclude": []}, budget=10**7)
    assert "SRCMARKER" in o and "MINIFIEDMARKER" in o and "LOCKMARKER" in o
    assert "blob.bin" in o.split("diff --git")[0]  # binaries are still summarised

    # oversized source diff → truncated per file with a marker, never an exception
    root2 = make_repo(big_source=True)
    budget = 100_000
    o = gate.review_diff(root2, {}, budget=budget)
    assert len(o) <= budget, len(o)
    assert "[truncated:" in o and "Truncated" in o
    head = o.split("diff --git")[0]
    assert "src/big1.py" in head and "src/big2.py" in head
    assert "src/a.py" in o and "SRCMARKER" in o  # small file untouched
    assert "MINIFIEDMARKER" not in o

    # tiny budget → backstop slice, still no exception
    o = gate.review_diff(root2, {}, budget=500)
    assert len(o) <= 500

    # not a git repo → degrades, no exception
    assert isinstance(gate.review_diff(Path(tempfile.mkdtemp()), {}), str)
    print("review_diff: ok")


main()

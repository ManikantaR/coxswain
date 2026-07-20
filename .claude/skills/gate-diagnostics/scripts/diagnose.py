#!/usr/bin/env python3
"""coxd gate/review self-diagnostics — bundles three checks that were, before this skill
existed, re-typed as ad-hoc one-liners every time something needed verifying:

1. Migration-journal parity for a worktree (same check the gate itself runs).
2. Diff-sanity: does gate.diff() actually span the full branch point, or is it
   accidentally scoped to just the last commit? (The exact bug that made two merged
   PRs' AI review silently see a 3-line fix instead of the real feature.)
3. Cost/turn history for a task (or all tasks) from the coxd sqlite store.

Usage:
    python3 diagnose.py migrations <worktree-path>
    python3 diagnose.py diff-sanity <worktree-path>
    python3 diagnose.py cost [task-id-substring]
    python3 diagnose.py all <worktree-path> [task-id-substring]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

COXD_DIR = Path(__file__).resolve().parents[4] / "coxd"
sys.path.insert(0, str(COXD_DIR))


def _store_path() -> Path:
    import os

    home = Path(os.environ.get("COXD_HOME", str(Path.home() / ".coxswain")))
    return home / "coxd.sqlite"


def check_migrations(worktree: str) -> None:
    import gate

    result = gate._check_migrations(Path(worktree))
    if result is None:
        print("SKIP: no drizzle journal found (not a drizzle repo)")
    elif result == "ok":
        print("OK: every migration .sql is registered in the journal")
    else:
        print(f"RED: {result}")


def check_diff_sanity(worktree: str) -> None:
    """Confirm gate.diff() spans the real branch point, not just the last commit.

    Cross-checks gate.diff()'s file/line count against `git diff <merge-base>...HEAD`
    directly. A mismatch means something is scoping the diff too narrowly — exactly
    the class of bug that silently fed the AI reviewer a near-empty diff after any
    fix-round (a second commit) landed on the branch.
    """
    import subprocess

    import gate

    wt = Path(worktree)
    d = gate.diff(wt)
    files_via_gate = d.count("diff --git")

    base = gate._base_ref(wt)
    r = subprocess.run(
        ["git", "diff", "--stat", f"{base}...HEAD"], cwd=wt, capture_output=True, text=True
    )
    # Last line of --stat is a summary like "3 files changed, ..." — count non-summary lines.
    files_via_direct = max(0, len([ln for ln in r.stdout.splitlines() if "|" in ln]))

    commits = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..HEAD"], cwd=wt, capture_output=True, text=True
    ).stdout.strip()

    print(f"branch point (base): {base}")
    print(f"commits since base:  {commits}")
    print(f"gate.diff() files:   {files_via_gate}  ({len(d)} chars)")
    print(f"git diff --stat files (direct, same base): {files_via_direct}")
    if files_via_gate != files_via_direct:
        print(
            "MISMATCH — gate.diff() does not match a direct diff against the same base. "
            "If commits-since-base > 1 and gate.diff()'s file count looks too small, "
            "this is the HEAD~1-default bug class (fixed in gate.diff(), verify the fix "
            "is actually present in this coxd checkout)."
        )
    elif int(commits or 0) > 1 and files_via_gate <= 1:
        print(
            "WARNING: multiple commits since base but gate.diff() shows <=1 file — "
            "worth a manual look even though the direct comparison matched."
        )
    else:
        print("OK: gate.diff() matches a direct diff against the same branch point.")


def check_cost(filter_substr: str | None = None) -> None:
    db = _store_path()
    if not db.exists():
        print(f"no store found at {db}")
        return
    conn = sqlite3.connect(db)
    where = ""
    params: tuple = ()
    if filter_substr:
        where = "WHERE id LIKE ?"
        params = (f"%{filter_substr}%",)
    rows = conn.execute(
        f"SELECT id, state, reason, cost, pr_url FROM tasks {where} ORDER BY rowid DESC LIMIT 20",
        params,
    ).fetchall()
    if not rows:
        print("no matching tasks")
        return
    print(f"{'task':<55} {'state':<12} {'cost':>8}  reason / pr")
    for tid, state, reason, cost, pr_url in rows:
        cost_s = f"${cost:.2f}" if cost else "$0.00"
        note = pr_url or reason or ""
        print(f"{tid[:55]:<55} {state:<12} {cost_s:>8}  {note}")

    if filter_substr:
        events = conn.execute(
            "SELECT kind, data FROM events WHERE task_id LIKE ? AND kind='result' ORDER BY seq",
            (f"%{filter_substr}%",),
        ).fetchall()
        if events:
            print("\nper-stage result events:")
            for _, data in events:
                d = json.loads(data)
                print(
                    f"  cost=${d.get('cost', 0):.4f}  turns={d.get('num_turns')}  "
                    f"stop_reason={d.get('stop_reason')}"
                )


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "migrations":
        check_migrations(sys.argv[2])
    elif cmd == "diff-sanity":
        check_diff_sanity(sys.argv[2])
    elif cmd == "cost":
        check_cost(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "all":
        wt = sys.argv[2]
        substr = sys.argv[3] if len(sys.argv) > 3 else None
        print("=== migrations ===")
        check_migrations(wt)
        print("\n=== diff-sanity ===")
        check_diff_sanity(wt)
        print("\n=== cost ===")
        check_cost(substr)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

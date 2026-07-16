"""V3.5 spike 2d — the assembled coxd loop (DESIGN-V35 §4).

Proves the whole nervous system stitched together: one task run as a single async
function through implement -> honest gate -> one review pass -> terminal, with
coxd as the SOLE state owner (SQLite) and an event log the board would tail. The
task is real (implement a function + a registry test command actually runs).
codex reviewer is a later swap; here worker+reviewer are cheap haiku.

Run from coxd/:  .venv/bin/python spike_loop.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-home-")  # isolate the store/registry

import loop  # noqa: E402
import registry  # noqa: E402
import store  # noqa: E402

TEST_CMD = ('python3 -c "import stats; assert stats.average([2,4,6])==4; '
            'assert stats.average([])==0; print(\'ok\')"')


def _init_repo() -> Path:
    d = Path(tempfile.mkdtemp(prefix="coxd-task-"))
    run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q")
    run("git", "config", "user.email", "c@c")
    run("git", "config", "user.name", "coxd")
    (d / "stats.py").write_text("# TODO: implement average(nums)\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "init")
    return d


async def main() -> int:
    wt = _init_repo()
    repo = "scratch-stats"
    # a real repo auto-scouts; this bare repo has no manifest, so seed the registry.
    registry.save(repo, {"test": TEST_CMD, "lint": "true", "target_branch": "main",
                         "source": "seeded (spike)"})
    tid = "scratch-1"
    store.create_task(
        tid, repo,
        "In stats.py implement `average(nums)` returning the arithmetic mean, and "
        "return 0 for an empty list. Then commit the change (do NOT push).",
        str(wt),
    )

    final = await loop.run_task(tid, worker_model="claude-haiku-4-5",
                                review_model="claude-haiku-4-5")

    print("\n=== event log (coxd is the single writer) ===")
    for e in store.events(tid):
        print(f"  [{e['kind']:10}] {str(e['data'])[:88]}")
    t = store.get_task(tid)
    print(f"\nfinal state: {t['state']}  reason={t['reason']}  cost=${t['cost'] or 0:.3f}")

    impl_ok = subprocess.run(["sh", "-c", TEST_CMD], cwd=wt, capture_output=True).returncode == 0
    ok = final == "pr_ready" and impl_ok
    print("VERDICT:", "✓ implement -> honest gate -> review -> pr_ready; single state owner"
          if ok else f"✗ investigate (state={final}, impl_ok={impl_ok})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

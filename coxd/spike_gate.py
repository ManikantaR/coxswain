"""V3.5 spike 2b — registry auto-scout + honest gate (DESIGN-V35 D23).

Proves the fix for the "gate lied" defect (#110 gate PASSED while skipping all
tests, because MyMoney had no .cox/repo.yml). Two proofs:
 1. `registry.scout()` discovers real commands from a repo's OWN manifests — no
    committed config. Run against the real MyMoney checkout.
 2. The gate is HONEST: GREEN when the test passes, RED when it fails, and RED
    (not "skip") when there is no test command. Deterministic, zero tokens.

Run: coxd/.venv/bin/python coxd/spike_gate.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import registry


def run_gate(worktree: Path, entry: dict, path: str = "full") -> dict:
    """Deterministic gate over a registry entry. A `full` task with no test/lint
    command is RED — never a silent skip (D23)."""
    steps: dict[str, str] = {}
    for name in ("test", "lint"):
        cmd = entry.get(name)
        if not cmd:
            if path == "full":
                return {"passed": False, "failing": name,
                        "reason": f"no {name} command in registry — RED, not skipped (D23)"}
            steps[name] = "skip"  # quick/inline tasks may legitimately skip
            continue
        r = subprocess.run(["sh", "-c", cmd], cwd=worktree, capture_output=True, text=True)
        steps[name] = "ok" if r.returncode == 0 else "red"
        if r.returncode != 0:
            return {"passed": False, "failing": name,
                    "reason": (r.stderr or r.stdout).strip()[:200], "steps": steps}
    return {"passed": True, "steps": steps}


def main() -> int:
    print("=== proof 1: scout the REAL MyMoney repo (no .cox/repo.yml) ===")
    mymoney = Path.home() / "repo" / "MyMoney"
    if mymoney.exists():
        e = registry.scout(mymoney)
        print(f"  discovered from {e['source']}: test={e['test']!r} lint={e['lint']!r} "
              f"build={e['build']!r}")
        scouted_ok = e["test"] is not None
    else:
        print("  (MyMoney checkout not found — skipping live scout)")
        scouted_ok = True

    print("\n=== proof 2: the gate is honest (GREEN / RED / RED-on-missing) ===")
    wt = Path("/tmp")  # commands are self-contained; cwd is irrelevant here
    green = run_gate(wt, {"test": "true", "lint": "true"})
    red = run_gate(wt, {"test": "false", "lint": "true"})
    missing_full = run_gate(wt, {"test": None, "lint": "true"}, path="full")
    missing_quick = run_gate(wt, {"test": None, "lint": "true"}, path="quick")
    print(f"  passing test  -> {green}")
    print(f"  failing test  -> {red}")
    print(f"  missing test (full)  -> {missing_full}")
    print(f"  missing test (quick) -> {missing_quick}")

    ok = (scouted_ok and green["passed"] and not red["passed"]
          and not missing_full["passed"]  # THE fix: missing != pass
          and missing_full["failing"] == "test"
          and missing_quick["passed"])  # quick may skip
    print("\nVERDICT:", "✓ registry scouts from manifests; gate cannot silently skip a full task"
          if ok else "✗ investigate")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

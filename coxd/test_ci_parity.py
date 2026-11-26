"""Gate/CI parity: flags a gate that differs from what the repo's CI workflow runs.

Standalone: cd coxd && COXD_HOME=$(mktemp -d) python test_ci_parity.py   (also runs under pytest)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-par-")
os.environ.pop("COXD_OWNER_USER", None)
os.environ.pop("COXD_PROXY_SECRET", None)

import board  # noqa: E402
import ci_parity  # noqa: E402
import registry  # noqa: E402
import store  # noqa: E402

CI = """\
name: ci
on: [push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install
        run: pip install -e ".[dev]"
      - name: Configure git
        run: |
          git config --global user.email ci@x.test
          git config --global user.name ci
      - run: ruff check .
      - run: mypy
      - name: Test
        run: python -m pytest -q
"""


def fixture_repo(ci: str = CI) -> Path:
    d = Path(tempfile.mkdtemp(prefix="coxd-par-repo-"))
    (d / ".github" / "workflows").mkdir(parents=True)
    (d / ".github" / "workflows" / "ci.yml").write_text(ci)
    return d


def test_flags_ci_step_the_gate_cannot_run():
    d = fixture_repo()
    r = ci_parity.check("fx", d, {"lint": "ruff check .", "test": "pytest -q", "build": None})
    assert r["ok"] is False and r["ci_only"] == ["mypy"] and r["mismatched"] == [], r
    assert "mypy" in r["note"] and "cannot run" in r["note"]


def test_matching_gate_is_ok():
    d = fixture_repo()
    r = ci_parity.check("fx", d, {"lint": "ruff check . && mypy", "test": "pytest -q"})
    assert r["ok"] is True and r["ci_only"] == [] and r["mismatched"] == [], r


def test_same_tool_different_args_is_a_mismatch():
    d = fixture_repo()
    r = ci_parity.check("fx", d, {"lint": "ruff check . && mypy", "test": "pytest"})
    assert r["ok"] is False and r["mismatched"] == [{"gate": "pytest", "ci": "pytest -q"}], r


def test_ignore_list_silences_a_deliberate_gap():
    d = fixture_repo()
    entry = {"lint": "ruff check .", "test": "pytest -q", "ci_parity_ignore": ["mypy"]}
    assert ci_parity.check("fx", d, entry)["ok"] is True


def test_node_package_manager_prefixes_normalize():
    d = fixture_repo("jobs:\n  a:\n    steps:\n      - run: pnpm install --frozen-lockfile\n"
                     "      - run: pnpm lint\n      - run: pnpm test\n"
                     "      - run: pnpm exec playwright install --with-deps\n"
                     "      - run: pnpm e2e\n")
    r = ci_parity.check("web", d, {"lint": "pnpm lint", "test": "pnpm test", "build": "pnpm build"})
    assert r["ci_only"] == ["e2e"] and r["mismatched"] == [], r


def test_regex_fallback_without_pyyaml():
    runs = ci_parity._scan_runs(CI)
    assert "ruff check ." in runs and "mypy" in runs and any("git config" in x for x in runs)
    saved, ci_parity._yaml = ci_parity._yaml, None
    try:
        r = ci_parity.check("fx", fixture_repo(), {"lint": "ruff check .", "test": "pytest -q"})
    finally:
        ci_parity._yaml = saved
    assert r["ci_only"] == ["mypy"], r


def test_board_payload_exposes_the_warning():
    d = fixture_repo()
    registry.save("fxrepo", {"lint": "ruff check .", "test": "pytest -q", "build": None})
    store.create_task("par1", "fxrepo", "brief", "/wt", repo_path=str(d))
    rows = {t["id"]: t for t in board._tasks_payload(fetch_queue=False)["tasks"]}
    p = rows["par1"]["ci_parity"]
    assert p and p["ci_only"] == ["mypy"] and "mypy" in p["note"], p
    registry.save("fxrepo", {"lint": "ruff check . && mypy", "test": "pytest -q"})
    rows = {t["id"]: t for t in board._tasks_payload(fetch_queue=False)["tasks"]}
    assert rows["par1"]["ci_parity"] is None  # gate changed -> cache refreshed, warning gone


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)

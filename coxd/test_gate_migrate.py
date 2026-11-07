"""Tests for the gate's `migrate-run` step. Standalone script (like the other coxd tests):

    cd coxd && COXD_HOME=$(mktemp -d) python test_gate_migrate.py

Fake-binary tests always run; the real-Postgres tests run only where PG binaries exist
(the coxd container) and the process is not root (Postgres refuses to run as root).
Invented data only.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("COXD_HOME", tempfile.mkdtemp(prefix="coxd-test-"))
os.environ.pop("COXD_WORKER_USER", None)  # run unprivileged-as-self, no setpriv

import envscrub  # noqa: E402
import gate  # noqa: E402

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(*a: str, cwd: Path) -> None:
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env={**os.environ, **ENV})


def fixture_repo(files: dict[str, str]) -> Path:
    """main has an init commit; branch `work` adds `files`."""
    p = Path(tempfile.mkdtemp(prefix="coxd-mig-"))
    git("init", "-q", "-b", "main", cwd=p)
    (p / "README").write_text("x")
    git("add", ".", cwd=p)
    git("commit", "-q", "-m", "init", cwd=p)
    git("checkout", "-q", "-b", "work", cwd=p)
    for name, body in files.items():
        (p / name).parent.mkdir(parents=True, exist_ok=True)
        (p / name).write_text(body)
    git("add", ".", cwd=p)
    git("commit", "-q", "-m", "mig", cwd=p)
    return p


def scratch_entries() -> set[str]:
    return {n for n in os.listdir(envscrub.scratch_home()) if n.startswith("m-")}


def fake_bins(psql_script: str) -> dict:
    d = Path(tempfile.mkdtemp(prefix="coxd-fakebin-"))
    scripts = {
        "initdb": 'while [ $# -gt 0 ]; do [ "$1" = -D ] && mkdir -p "$2"; shift; done',
        "pg_ctl": "exit 0",
        "psql": psql_script,
    }
    out = {}
    for n, body in scripts.items():
        f = d / n
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
        out[n] = str(f)
    return out


def run(repo: Path, migrate: dict | None) -> dict:
    entry = {"test": "true", "lint": "true", "build": False}
    if migrate is not None:
        entry["migrate"] = migrate
    return gate.run_gate(repo, entry)


def real_pg() -> bool:
    return os.getuid() != 0 and gate._pg_bins(os.environ) is not None


def main() -> int:
    fails = 0

    def check(name: str, cond: bool, detail: object = "") -> None:
        nonlocal fails
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
        fails += 0 if cond else 1

    # 1. without `migrate` the gate is unchanged
    repo = fixture_repo({"a.sql": "select 1;\n"})
    g = run(repo, None)
    check("no migrate -> unchanged", g["passed"] and "migrate-run" not in g["steps"], g)

    # 2. missing binaries -> RED with a clear reason, no crash
    real = gate._pg_bins
    gate._pg_bins = lambda env: None
    try:
        before = scratch_entries()
        g = run(repo, {"cmd": "true", "url_env": "DATABASE_URL"})
        check("missing binaries -> RED", not g["passed"] and g["failing"] == "migrate-run"
              and "not found" in g["reason"], g)
        check("missing binaries leave no temp dir", scratch_entries() == before)
    finally:
        gate._pg_bins = real

    # 3. fake psql failing -> RED with error text (cut to 1 KB); temp dir removed;
    #    cmd sees url_env pointing at a socket and no real DB URL
    os.environ["DATABASE_URL"] = "postgresql://real.example.invalid/prod"
    noisy = 'echo "$(printf x%.0s $(seq 1 2000)) ERROR: boom" >&2; exit 1'
    gate._pg_bins = lambda env: fake_bins(noisy)
    try:
        before = scratch_entries()
        g = run(repo, {"cmd": "true", "url_env": "DATABASE_URL"})
        check("failing statement -> RED with error text",
              not g["passed"] and g["failing"] == "migrate-run" and "ERROR: boom" in g["reason"], g)
        check("reason cut to 1KB", len(g["reason"].split(": ", 1)[-1]) <= 1024, len(g["reason"]))
        check("temp dir removed after failure", scratch_entries() == before)
    finally:
        gate._pg_bins = real

    # 4. fake success path: cmd must see the socket URL, not the real one; dir removed
    gate._pg_bins = lambda env: fake_bins("exit 0")
    try:
        before = scratch_entries()
        out = Path(tempfile.mkdtemp(prefix="coxd-out-")) / "url"
        g = run(repo, {"cmd": f'printf %s "$DATABASE_URL" > {out}', "url_env": "DATABASE_URL"})
        seen = out.read_text() if out.exists() else ""
        check("success path ok", g["passed"] and g["steps"]["migrate-run"] == "ok", g)
        check("cmd sees socket URL only", "real.example" not in seen and "host=" in seen, seen)
        check("temp dir removed after success", scratch_entries() == before)
    finally:
        gate._pg_bins = real
        os.environ.pop("DATABASE_URL", None)

    # 5. timeout stays RED and cleans up
    gate._pg_bins = lambda env: fake_bins("exit 0")
    old = gate.MIGRATE_TIMEOUT
    gate.MIGRATE_TIMEOUT = 1
    try:
        before = scratch_entries()
        g = run(repo, {"cmd": "sleep 30", "url_env": "DATABASE_URL"})
        check("timeout -> RED", not g["passed"] and "timed out" in g["reason"], g)
        check("temp dir removed after timeout", scratch_entries() == before)
    finally:
        gate.MIGRATE_TIMEOUT = old
        gate._pg_bins = real

    # 6. real Postgres: safe-to-rerun migration passes; plain CREATE TABLE goes RED on re-run
    if real_pg():
        safe = "CREATE TABLE IF NOT EXISTS widgets (id int);\n"
        unsafe = "CREATE TABLE gadgets (id int);\n"
        cmd = ('for f in db/migrations/*.sql; do '
               'psql -X -v ON_ERROR_STOP=1 "$DATABASE_URL" -f "$f" || exit 1; done')
        cfg = {"cmd": cmd, "url_env": "DATABASE_URL", "extensions": []}
        before = scratch_entries()
        g = run(fixture_repo({"db/migrations/0001_safe.sql": safe}), cfg)
        check("real PG: re-runnable migration ok", g["passed"], g)
        g = run(fixture_repo({"db/migrations/0001_safe.sql": safe,
                              "db/migrations/0002_unsafe.sql": unsafe}), cfg)
        check("real PG: non-rerunnable -> RED on re-run",
              not g["passed"] and "re-run" in g["reason"] and "already exists" in g["reason"], g)
        g = run(fixture_repo({"db/migrations/0001_bad.sql": "SELECT * FROM nope;\n"}), cfg)
        check("real PG: failing statement -> RED with text",
              not g["passed"] and "nope" in g["reason"], g)
        check("real PG: temp dirs removed", scratch_entries() == before)
    else:
        print("SKIP real-Postgres tests (no PG binaries or running as root)")

    print("FAILED" if fails else "ALL OK")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

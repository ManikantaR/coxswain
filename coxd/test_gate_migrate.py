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

# private tmp so envscrub.scratch_home() (chmod 1777) is ours, never a shared /tmp dir
tempfile.tempdir = tempfile.mkdtemp(prefix="coxd-tmp-")

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
        # foreground "server": record our pid, plant a pid file naming a canary, then block
        "postgres": ('while [ $# -gt 0 ]; do [ "$1" = -D ] && D="$2"; shift; done\n'
                     'echo $$ > "$FAKE_PG_PID_OUT"\n'
                     '[ -n "$CANARY_PID" ] && echo "$CANARY_PID" > "$D/postmaster.pid"\n'
                     'exec sleep 300'),
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
        entry["migrate"] = {"dir": "db/migrations", **migrate}
    return gate.run_gate(repo, entry)


def real_pg() -> bool:
    return os.getuid() != 0 and gate._pg_bins(os.environ) is not None


def main() -> int:
    fails = 0
    os.environ["FAKE_PG_PID_OUT"] = os.path.join(tempfile.mkdtemp(prefix="coxd-pid0-"), "p")

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
    noisy = ('case "$*" in *"SELECT 1"*) exit 0;; esac\n'
             'echo "$(printf x%.0s $(seq 1 2000)) ERROR: boom" >&2; exit 1')
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

    # 4b. env scrub: every DB-looking var is gone, url_env is the throwaway socket
    gate._pg_bins = lambda env: fake_bins("exit 0")
    leaks = {"DATABASE_URL": "x", "APP_DATABASE_URL": "x", "MAIN_DB_URL": "x", "PGHOST": "x",
             "PGPASSWORD": "x", "MY_URL": "x"}
    os.environ.update(leaks)
    try:
        out = Path(tempfile.mkdtemp(prefix="coxd-out-")) / "env"
        g = run(repo, {"cmd": f"env > {out}", "url_env": "MY_URL"})
        seen = out.read_text() if out.exists() else ""
        names = {ln.split("=", 1)[0] for ln in seen.splitlines()}
        check("db vars stripped", g["passed"] and not (names & set(leaks) - {"MY_URL"}), names)
        check("url_env points at socket", "MY_URL=postgresql://postgres@/app?host=" in seen)
    finally:
        for k in leaks:
            os.environ.pop(k, None)
        gate._pg_bins = real

    # 4c. root never kills via a (worker-writable) pid file; recorded pgid is used instead.
    #     fake pg_ctl cannot stop the server, so the pgid kill path must fire.
    canary = subprocess.Popen(["sleep", "60"])
    pid_out = Path(tempfile.mkdtemp(prefix="coxd-pid-")) / "pg.pid"
    os.environ["CANARY_PID"] = str(canary.pid)
    os.environ["FAKE_PG_PID_OUT"] = str(pid_out)
    gate._pg_bins = lambda env: fake_bins("exit 0")
    try:
        before = scratch_entries()
        g = run(repo, {"cmd": "true"})
        pg_pid = int(pid_out.read_text())
        check("pid-file run ok", g["passed"], g)
        check("canary PID from pid file NOT killed", canary.poll() is None)
        try:
            os.kill(pg_pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        check("started server group killed via recorded pgid", not alive, pg_pid)
        check("temp dir removed (pid file case)", scratch_entries() == before)
    finally:
        canary.kill()
        canary.wait()
        os.environ.pop("CANARY_PID", None)
        gate._pg_bins = real

    # 4d. re-run only .sql files added directly in migrate.dir; seeds/templates ignored
    ran = Path(tempfile.mkdtemp(prefix="coxd-ran-")) / "log"
    psql_log = f'case "$*" in *"-f "*) echo "$*" >> {ran};; esac; exit 0'
    gate._pg_bins = lambda env: fake_bins(psql_log)
    try:
        r2 = fixture_repo({"db/migrations/0001_a.sql": "select 1;\n",
                           "db/migrations/sub/0002_nested.sql": "select 1;\n",
                           "db/seeds/seed.sql": "select 1;\n",
                           "app/queries/report.sql": "select 1;\n"})
        g = run(r2, {"cmd": "true"})
        log = ran.read_text() if ran.exists() else ""
        check("only files in migrate.dir re-run",
              g["passed"] and "0001_a.sql" in log and "seed.sql" not in log
              and "report.sql" not in log and "nested" not in log, log)
        g = run(r2, {"cmd": "true", "dir": ""})
        check("missing migrate.dir -> RED", not g["passed"] and "migrate.dir" in g["reason"], g)
    finally:
        gate._pg_bins = real

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

"""Deterministic gate — DESIGN-V35 (survives the pivot; policy, not transport).

Runs the registry's commands. A `full` task with no test/lint command is RED,
never a silent "skip" (the #99 "gate lied" defect). Zero tokens.
"""

from __future__ import annotations

import glob
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import envscrub
import sandbox


def _check_migrations(worktree: Path) -> str | None:
    """Drizzle journal parity — the gate's e2e blind-spot fix.

    Every migration .sql must be registered in db/migrations/meta/_journal.json,
    or `drizzle-kit migrate` silently stops at the last journaled entry and the
    new columns never exist. Unit tests + `nest build` stay green (they don't hit
    a migrated DB) while CI e2e 500s on missing columns — the #101 defect, and
    #113 (0013) before it: 2 of 2 manual unsticks were this exact miss. Cheap,
    deterministic, zero tokens. Returns None when the repo has no drizzle journal.
    """
    journal = worktree / "db" / "migrations" / "meta" / "_journal.json"
    mig_dir = worktree / "db" / "migrations"
    if not journal.exists():
        return None  # not a drizzle repo — nothing to check
    try:
        entries = json.loads(journal.read_text(encoding="utf-8")).get("entries", [])
    except (ValueError, OSError) as e:
        return f"unreadable drizzle journal ({e})"
    tags = {e.get("tag") for e in entries}
    files = {p.stem for p in mig_dir.glob("*.sql")}
    unregistered = sorted(files - tags)
    if unregistered:
        return ("migration .sql not registered in drizzle journal — drizzle-kit "
                "migrate will skip it, leaving the columns missing: "
                + ", ".join(unregistered))
    orphaned = sorted(t for t in tags if t and t not in files)
    if orphaned:
        return ("drizzle journal references a missing .sql file: " + ", ".join(orphaned))
    return "ok"


MIGRATE_TIMEOUT = 300  # seconds, whole migrate-run step
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _Timeout(Exception):
    pass


class _Red(Exception):
    pass


def _tail(text: str) -> str:
    return (text or "").strip()[-1024:]


def _is_db_var(name: str) -> bool:
    """Variables that could carry a real database location or credentials."""
    return (name.startswith("PG") or name == "DATABASE_URL"
            or name.endswith(("_DATABASE_URL", "_DB_URL")))


def _pg_bins(env: dict) -> dict | None:
    """Absolute paths of initdb/pg_ctl/postgres/psql (PATH first, then the PGDG layout)."""
    found = {}
    dirs = sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    for name in ("initdb", "pg_ctl", "postgres", "psql"):
        p = shutil.which(name, path=env.get("PATH"))
        if not p:
            for d in dirs:
                if os.access(os.path.join(d, name), os.X_OK):
                    p = os.path.join(d, name)
                    break
        if not p:
            return None
        found[name] = p
    return found


def _run(argv: list[str], env: dict, deadline: float, cwd=None):
    """Run as the worker user in its own process group; kill the group on timeout."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Timeout()
    p = subprocess.Popen(sandbox.wrap(argv), cwd=cwd, env=env, text=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = p.communicate(timeout=remaining)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        raise _Timeout() from None
    return subprocess.CompletedProcess(argv, p.returncode, out, err)


def _added_sql(worktree: Path, mdir: str) -> list[str]:
    base = _base_ref(worktree)
    r = sandbox.run_in_worktree(
        ["git", "diff", "--name-only", "--diff-filter=A", f"{base}...HEAD", "--", "*.sql"],
        worktree)
    if r.returncode != 0:  # e.g. no merge base — fall back to a two-dot diff
        r = sandbox.run_in_worktree(
            ["git", "diff", "--name-only", "--diff-filter=A", base, "HEAD", "--", "*.sql"],
            worktree)
    if r.returncode != 0:
        raise _Red("cannot list migrations added by this branch: " + _tail(r.stderr))
    # only .sql files added DIRECTLY in the migrations dir; seeds/templates elsewhere are ignored
    return sorted(ln for ln in r.stdout.splitlines()
                  if ln.endswith(".sql") and posixpath.dirname(ln) == mdir)


def _read_fd(fd: int) -> str:
    """Tail of the log via the fd root opened itself — never by path (worker-writable dir)."""
    try:
        size = os.fstat(fd).st_size
        return os.pread(fd, 2048, max(0, size - 2048)).decode("utf-8", "replace")
    except OSError:
        return ""


def _stop_postgres(pg: subprocess.Popen, pgid: int, bins: dict, data: str, env: dict) -> None:
    """Stop the server we started: pg_ctl fast, then immediate (both as the worker), then
    SIGKILL the recorded process group if it is still ours and still running."""
    for mode in ("fast", "immediate"):
        if pg.poll() is not None:
            break
        try:
            subprocess.run(sandbox.wrap([bins["pg_ctl"], "-D", data, "-m", mode,
                                         "-w", "-t", "10", "stop"]),
                           env=env, capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        except Exception:
            pass
        try:
            pg.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    if pg.poll() is None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    try:
        pg.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _migrate_run(worktree: Path, entry: dict, env: dict) -> str | None:
    """Throwaway-Postgres migration run. Returns None when ok, else the RED reason."""
    cfg = entry["migrate"]
    if not isinstance(cfg, dict):
        return "registry `migrate` must be an object — RED"
    cmd, url_env = cfg.get("cmd"), cfg.get("url_env") or "DATABASE_URL"
    exts = cfg.get("extensions") or []
    if not isinstance(cmd, str) or not cmd.strip():
        return "migrate.cmd missing in registry — RED"
    if not isinstance(url_env, str) or not _NAME_RE.match(url_env):
        return f"migrate.url_env is not a valid environment variable name: {url_env!r}"
    mdir = cfg.get("dir")
    if not isinstance(mdir, str) or not mdir.strip():
        return "migrate.dir missing in registry (e.g. db/migrations) — RED"
    mdir = posixpath.normpath(mdir.strip())
    if mdir.startswith("/") or mdir == ".." or mdir.startswith("../"):
        return "migrate.dir must be a relative path inside the repo"
    if not isinstance(exts, list) or not all(
            isinstance(x, str) and _NAME_RE.match(x) for x in exts):
        return "migrate.extensions must be a list of plain extension names"
    # No stray PG* settings or real DB URLs may reach the throwaway server or the command.
    env = {k: v for k, v in env.items() if not _is_db_var(k) and k != url_env}
    bins = _pg_bins(env)
    if not bins:
        return ("PostgreSQL binaries (initdb, pg_ctl, postgres, psql) not found — install "
                "postgresql-16 (see deploy/Dockerfile); cannot run migrations")
    deadline = time.monotonic() + MIGRATE_TIMEOUT
    tmp = None
    pg = None
    pgid = -1
    logfd = -1
    try:
        tmp = tempfile.mkdtemp(prefix="m-", dir=envscrub.scratch_home())
        # Open the log as root BEFORE the dir is handed to the worker, refusing symlinks;
        # postgres inherits the fd, and root reads it back through the fd, never the path.
        logfd = os.open(os.path.join(tmp, "pg.log"),
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        sandbox.own(Path(tmp))
        data = os.path.join(tmp, "data")
        r = _run([bins["initdb"], "-D", data, "-U", "postgres", "-A", "trust",
                  "-E", "UTF8", "--no-locale", "--no-sync"], env, deadline)
        if r.returncode != 0:
            return "initdb failed: " + _tail(r.stderr or r.stdout)
        # Run postgres in the foreground as the worker user, in its own process group.
        # Root only ever signals the pgid it recorded here — never anything read from the
        # worker-owned data dir (postmaster.pid).
        pg = subprocess.Popen(
            sandbox.wrap([bins["postgres"], "-D", data, "-c", "listen_addresses=",
                          "-c", f"unix_socket_directories={tmp}", "-c", "fsync=off"]),
            env=env, stdin=subprocess.DEVNULL, stdout=logfd, stderr=logfd,
            start_new_session=True)
        pgid = pg.pid  # session leader => pgid == pid
        host = quote(tmp, safe="")
        base_psql = [bins["psql"], "-X", "-q", "-v", "ON_ERROR_STOP=1"]
        while True:  # wait for the server to accept connections
            if pg.poll() is not None:
                return "postgres failed to start: " + _tail(_read_fd(logfd))
            r = _run(base_psql + [f"postgresql://postgres@/postgres?host={host}",
                                  "-c", "SELECT 1"], env, deadline)
            if r.returncode == 0:
                break
            time.sleep(0.2)
        url = f"postgresql://postgres@/app?host={host}"
        r = _run(base_psql + [f"postgresql://postgres@/postgres?host={host}",
                              "-c", "CREATE DATABASE app"], env, deadline)
        if r.returncode != 0:
            return "create database failed: " + _tail(r.stderr or r.stdout)
        for ext in exts:
            r = _run(base_psql + [url, "-c", f'CREATE EXTENSION IF NOT EXISTS "{ext}"'],
                     env, deadline)
            if r.returncode != 0:
                return f"CREATE EXTENSION {ext} failed: " + _tail(r.stderr or r.stdout)
        r = _run(["sh", "-c", cmd], {**env, url_env: url}, deadline, cwd=worktree)
        if r.returncode != 0:
            return "migrate cmd failed: " + _tail(r.stderr or r.stdout)
        # Re-run check: every .sql this branch adds must apply a second time.
        root = worktree.resolve()
        for rel in _added_sql(worktree, mdir):
            f = (worktree / rel).resolve()
            if not f.is_relative_to(root) or not f.is_file():
                continue
            r = _run(base_psql + ["--single-transaction", "-f", str(f), url], env, deadline)
            if r.returncode != 0:
                return f"re-run of {rel} failed: " + _tail(r.stderr or r.stdout)
        return None
    except _Red as e:
        return str(e)
    except _Timeout:
        return f"migrate-run timed out after {MIGRATE_TIMEOUT}s"
    finally:
        if tmp:
            if pg is not None:
                _stop_postgres(pg, pgid, bins, os.path.join(tmp, "data"), env)
            if logfd >= 0:
                os.close(logfd)
            shutil.rmtree(tmp, ignore_errors=True)


def _base_ref(worktree: Path) -> str:
    """The branch point to diff against — for turbo's `--filter=[<ref>]` affected scope."""
    for ref in ("origin/main", "origin/master", "main", "master"):
        r = sandbox.run_in_worktree(["git", "rev-parse", "--verify", "-q", ref], worktree)
        if r.returncode == 0:
            return ref
    return "HEAD~1"


def run_gate(worktree: Path, entry: dict, path: str = "full") -> dict:
    # turbo monorepo → scope every step to the packages CHANGED vs the branch point,
    # so an unrelated package can't fail (or OOM) a task that never touched it.
    turbo = entry.get("runner") == "turbo"
    base = _base_ref(worktree) if turbo else None
    # Replicate the repo's CI-critical env so the gate matches CI, not a bare shell.
    # apps/web's jsdom suite OOMs without CI's NODE_OPTIONS heap bump — without this the
    # gate forces workers to hack package.json / drop tests to get green (the #101 defect).
    env = envscrub.scrubbed_env(entry.get("gate_env") or {})  # gate runs worker-written code
    steps: dict[str, str] = {}
    # migration-registration parity FIRST (fail fast, cheap): closes the gate's e2e
    # blind spot where unregistered drizzle migrations pass unit+build but 500 in CI.
    mig = _check_migrations(worktree)
    if mig is not None:
        steps["migrations"] = "ok" if mig == "ok" else "red"
        if mig != "ok":
            return {"passed": False, "failing": "migrations", "steps": steps, "reason": mig}
    if entry.get("migrate"):
        why = _migrate_run(worktree, entry, env)
        steps["migrate-run"] = "ok" if why is None else "red"
        if why is not None:
            return {"passed": False, "failing": "migrate-run", "steps": steps, "reason": why}
    # build catches what vitest/esbuild can't: `tsc` type errors that pass tests but
    # break `nest build` (the #112 lesson, re-proven on #100). Required-ness differs:
    # test/lint absent = UNKNOWN → RED; build absent = genuinely optional → skip.
    test_output = ""
    for name in ("test", "lint", "build"):
        cmd = entry.get(name)
        if cmd is False:  # deliberately no gate here (repo's CI doesn't run it) — NOT unknown
            steps[name] = "none"
            continue
        if not cmd:  # None/"" — RED for test/lint on a full task (don't lie); build is optional
            if path == "full" and name != "build":
                return {"passed": False, "failing": name,
                        "reason": f"no {name} command in registry — RED, not skipped"}
            steps[name] = "skip"
            continue
        if turbo:  # deterministic path to the local turbo bin; affected-only scope
            cmd = f"node_modules/.bin/turbo run {name} --filter='[{base}]'"
        r = subprocess.run(sandbox.wrap(["sh", "-c", cmd]), cwd=worktree,
                           capture_output=True, text=True, env=env)
        steps[name] = "ok" if r.returncode == 0 else "red"
        if r.returncode != 0:
            return {"passed": False, "failing": name, "steps": steps,
                    "reason": (r.stderr or r.stdout).strip()[-800:]}
        # Runner summary line (e.g. vitest's "Tests  606 passed (606)") is the cheapest
        # honest pass-count evidence — no per-runner parsing, just keep the real tail.
        if name == "test":
            test_output = (r.stdout or r.stderr).strip()[-1500:]
    return {"passed": True, "steps": steps, "test_output": test_output}


def diff(worktree: Path, base: str | None = None) -> str:
    """The FULL diff since the branch point — never just the last commit.

    Defaulted to `base="HEAD~1"` before, which only happens to be correct when the
    branch has exactly one commit. Any internal fix-round adds a second commit, and
    `HEAD~1` then points at the FEATURE commit itself — the reviewer silently saw only
    the tiny fix-round diff (e.g. 474 bytes on #107) instead of the real feature, and
    still burned turns fumbling over a diff that didn't match the review prompt's own
    framing. Use the same branch-point resolution the gate already uses for turbo
    scoping, so review always sees everything since origin/main, regardless of how
    many commits (fix-rounds) are on the branch.
    """
    base = base or _base_ref(worktree)
    r = sandbox.run_in_worktree(["git", "diff", f"{base}...HEAD"], worktree)
    if r.returncode != 0:  # e.g. no common ancestor — fall back to the last commit's diff
        r = sandbox.run_in_worktree(["git", "show", "HEAD"], worktree)
    return r.stdout

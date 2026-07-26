"""Post-ship CI watch + flake triage (DESIGN-V35 extension).

Encodes the ci-flake-triage protocol (previously run by hand every PR tonight)
directly into the loop: after `ship()` opens a PR, poll its GitHub Actions checks;
classify a red check as a known-flake signature (safe to rerun) or a real-looking
failure (escalate to the human, never silently retried into the ground).

Never invents a merge decision — this only decides whether the PR is worth
presenting to the human as pr_ready, or should stay in a working state a bit
longer while a flake clears.
"""

from __future__ import annotations

import re
import subprocess
import time

# Confirmed tonight (real MoneyPulse CI runs), not guessed: a hook-timeout in one
# e2e spec file cascades into unrelated 401/403s in NEXT tests in that file, and a
# DIFFERENT spec file fails on each rerun — the tell that distinguishes this from a
# real regression, which fails the same file deterministically every time.
_FLAKE_SIGNATURES = [
    re.compile(r"Hook timed out in \d+ms"),
    re.compile(r'Invalid value "undefined" for header "Cookie"'),
    re.compile(r"error connecting to results-receiver\.actions\.githubusercontent\.com"),
    re.compile(r"RequestError \[HttpError\]"),
]

# A variant of the same family, seen on #118's and #122's first CI runs: a shared
# beforeAll (register+login) silently yields empty cookies, so EVERY OTHER test in
# that e2e file gets a 401 it shouldn't. One or two 401s can be a real bug; a whole
# file cascading is the tell. Count-based, not a bare presence check, specifically
# so a single genuine auth regression doesn't get silently retried away.
_AUTH_CASCADE_PATTERN = re.compile(r'expected 200 "OK", got 40[13] "(Unauthorized|Forbidden)"')
_AUTH_CASCADE_MIN_COUNT = 3

_POLL_INTERVAL_S = 15
_MAX_WAIT_S = 600  # 10 min — CI here has run up to ~5 min for Docker Build alone
_MAX_RERUNS = 2


def _run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def repo_slug(repo_path) -> str | None:
    """'owner/repo' from the origin remote, or None (e.g. a local scratch repo)."""
    r = _run(["git", "remote", "get-url", "origin"], cwd=str(repo_path))
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", r.stdout.strip())
    return m.group(1) if m else None


def _checks(pr_number: str, repo_slug: str) -> list[dict]:
    r = _run(["gh", "pr", "checks", pr_number, "-R", repo_slug, "--json",
             "name,state,link,bucket"])
    if r.returncode != 0:
        return []
    import json
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return []


def _wait_for_resolution(pr_number: str, repo_slug: str) -> list[dict]:
    """Poll until nothing is pending, or _MAX_WAIT_S elapses (returns whatever the
    last poll saw — an unresolved 'pending' check after the timeout is treated the
    same as a failure by the caller, so it never hangs pr_ready indefinitely)."""
    deadline = time.time() + _MAX_WAIT_S
    checks = _checks(pr_number, repo_slug)
    while time.time() < deadline:
        if checks and all(c.get("bucket") != "pending" for c in checks):
            return checks
        time.sleep(_POLL_INTERVAL_S)
        checks = _checks(pr_number, repo_slug)
    return checks


def _run_id_from_link(link: str) -> str | None:
    m = re.search(r"/actions/runs/(\d+)", link or "")
    return m.group(1) if m else None


def _job_log_matches_flake(repo_slug: str, run_id: str) -> bool:
    # `gh api .../logs` returns a ZIP archive of every job's log, not text — must be
    # unzipped in memory before matching. Fetching with text=True here would crash
    # decoding the binary payload as UTF-8 (confirmed: this silently broke every
    # flake check until caught here).
    r = subprocess.run(
        ["gh", "api", f"repos/{repo_slug}/actions/runs/{run_id}/logs"],
        capture_output=True,
    )
    if r.returncode != 0:
        return False
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(r.stdout)) as zf:
            text = "\n".join(
                zf.read(name).decode("utf-8", errors="replace")
                for name in zf.namelist()
            )
    except zipfile.BadZipFile:
        return False
    if any(sig.search(text) for sig in _FLAKE_SIGNATURES):
        return True
    return len(_AUTH_CASCADE_PATTERN.findall(text)) >= _AUTH_CASCADE_MIN_COUNT


def _diff_touches_e2e(wt) -> bool:
    """If the PR's own diff touches e2e test files, don't blindly trust the flake
    signature — a real regression can ALSO happen to produce a similar-looking
    cascade. Lean toward escalating rather than auto-retrying in that case."""
    import gate
    base = gate._base_ref(wt)
    r = _run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=str(wt))
    return any("e2e" in ln or "/test/" in ln for ln in r.stdout.splitlines())


def watch_and_triage(pr_url: str, repo_slug: str, wt) -> tuple[str, str]:
    """Returns (outcome, detail). outcome: 'green' | 'flaky-cleared' | 'needs_human'."""
    pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]
    reruns = 0
    while True:
        checks = _wait_for_resolution(pr_number, repo_slug)
        if not checks:
            return ("needs_human", "ci-checks-unavailable")
        failing = [c for c in checks if c.get("bucket") == "fail"]
        if not failing:
            return ("green", "all checks passed")
        if reruns >= _MAX_RERUNS:
            return ("needs_human", f"ci-red after {reruns} rerun(s): "
                                   + ", ".join(c["name"] for c in failing))
        if _diff_touches_e2e(wt):
            return ("needs_human", "ci-red and this PR's own diff touches e2e/test "
                                   "files — not auto-retrying a change that could "
                                   "plausibly be a real regression in that area")
        run_ids = {_run_id_from_link(c.get("link", "")) for c in failing}
        run_ids.discard(None)
        if not any(_job_log_matches_flake(repo_slug, rid) for rid in run_ids if rid):
            return ("needs_human", "ci-red, does not match a known flake signature: "
                                   + ", ".join(c["name"] for c in failing))
        for rid in run_ids:
            if rid:
                _run(["gh", "run", "rerun", rid, "--failed", "-R", repo_slug])
        reruns += 1

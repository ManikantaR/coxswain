"""Post-merge cleanup poller (DESIGN-V35 extension).

Every PR merge tonight needed the same three manual steps afterward: remove the
worktree, delete the local+remote coxd/<id> branch, close the issue if it didn't
auto-link. None of that requires human judgment — it's pure bookkeeping once a
human has already made the one real decision (the merge itself). Detect the merge
and do the bookkeeping automatically instead of repeating it by hand every time.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

import ci_triage
import gitident
import greenlight
import notify
import registry
import store
import worktree

_ISSUE_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)")

# One deploy per repo at a time — a docker build takes minutes, so a second tap of
# the board's Deploy button (which bypasses the coalesce window on purpose) must not
# race a build already in flight. Keyed by repo name.
_deploy_inflight: set[str] = set()
_deploy_lock = threading.Lock()


def _run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


# A deploy (docker build of a monorepo on the NAS) legitimately takes many minutes, but
# never hours. Past this, kill it and report deploy-failed instead of hanging forever.
DEFAULT_DEPLOY_TIMEOUT_S = 45 * 60
_PROGRESS_EVERY_S = 20.0


DEFAULT_DEPLOYER_URL = "http://deployer:8792"


class DeployerError(Exception):
    """The deployer refused or could not be reached; message is user-facing."""

    def __init__(self, msg: str, code: int | None = None) -> None:
        super().__init__(msg)
        self.code = code


def _deployer_req(method: str, path: str, timeout: float = 30.0):
    token = os.environ.get("COXD_DEPLOYER_TOKEN", "")
    if not token:
        raise DeployerError("COXD_DEPLOYER_TOKEN is not set — the deployer is not configured")
    base = (os.environ.get("COXD_DEPLOYER_URL") or DEFAULT_DEPLOYER_URL).rstrip("/")
    req = urllib.request.Request(base + path, method=method,
                                 headers={"Authorization": f"Bearer {token}"},
                                 data=b"" if method == "POST" else None)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = {401: "bad deployer token", 404: "repo is not in the deployer allowlist",
                  409: "a deploy for this repo is already running"}.get(e.code, f"HTTP {e.code}")
        raise DeployerError(f"deployer refused: {detail}", e.code) from e
    except (urllib.error.URLError, OSError) as e:
        raise DeployerError(f"deployer unreachable: {e}") from e


def _deployer_status(jid: str) -> dict:
    with _deployer_req("GET", f"/deploys/{jid}/status") as r:
        try:
            return json.loads(r.read())
        except (OSError, http.client.HTTPException, ValueError) as e:
            # The job exists; only this poll failed. Callers retry until their deadline.
            raise DeployerError(f"deployer status unreadable: {e}") from e


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _stream_deploy(tid: str, repo: str, timeout_s: float, sha: str,
                   on_start=None) -> tuple[int, str]:
    """Trigger a deploy through the deployer container and relay its log.

    coxd has no docker.sock; the deployer runs the allowlisted command for `repo`. Every
    log line is echoed to coxd's stdout, a `deploy-progress` event carries the latest line
    every ~20s, and we give up after `timeout_s`. Returns (exit_code, tail-of-output);
    rc=124 on timeout, 127 if the deployer could not be reached or refused. `sha` is the
    commit the deployer must end up building (it verifies HEAD; mismatch -> stale-source).
    `on_start` runs once the deployer has accepted the job (not on 409/refusal)."""
    if not _SHA_RE.match(sha or ""):
        return 127, f"could not start deploy: bad commit sha {sha!r}"
    path = (f"/deploy/{urllib.parse.quote(repo, safe='')}/{sha}"
            f"?timeout_s={int(timeout_s)}")  # same timeout on both sides: they can't disagree
    try:
        with _deployer_req("POST", path) as r:
            try:
                jid = json.loads(r.read())["id"]
            except (OSError, http.client.HTTPException) as e:
                # Accepted (202 headers arrived) but the body was lost: the job may be running.
                return 127, ("deploy may have started: could not read the deployer's response "
                             f"({e}) - check the deployer before retrying")
    except DeployerError as e:
        if e.code == 409:  # a deploy is already in flight — not a failure of this one
            return 409, f"{e}"
        return 127, f"could not start deploy: {e}"
    except (ValueError, KeyError) as e:
        return 127, f"could not start deploy: bad deployer response ({e})"
    if on_start:
        on_start()
    deadline = time.monotonic() + timeout_s
    tail: deque[str] = deque(maxlen=40)
    stop = threading.Event()  # set on return: a stale relay must emit nothing afterwards

    def _relay() -> None:
        last_emit = time.monotonic()
        try:
            with _deployer_req("GET", f"/deploys/{jid}/log", timeout=timeout_s + 30) as r:
                for raw in r:
                    if stop.is_set():
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    tail.append(line)
                    print(f"[deploy {tid}] {line}", flush=True)
                    if time.monotonic() - last_emit >= _PROGRESS_EVERY_S:
                        store.append_event(tid, "deploy-progress", {"line": line[-200:]})
                        last_emit = time.monotonic()
        except (DeployerError, OSError, ValueError, http.client.HTTPException) as e:
            tail.append(f"(log stream lost: {e})")

    th = threading.Thread(target=_relay, daemon=True)
    th.start()
    try:
        th.join(max(0.0, deadline - time.monotonic()))
        while True:
            try:
                st = _deployer_status(jid)
            except (DeployerError, ValueError, OSError, http.client.HTTPException) as e:
                if isinstance(e, DeployerError) and e.code == 404:  # deployer restarted: job gone
                    return 127, "deployer no longer knows this job (restarted?)\n" + "\n".join(tail)
                if time.monotonic() >= deadline:
                    return 124, f"timed out after {int(timeout_s)}s ({e})\n" + "\n".join(tail)
                time.sleep(2)
                continue
            if st.get("status") != "running":
                th.join(5)  # let the relay drain the last lines
                code = st.get("exit_code")
                code = code if isinstance(code, int) else -1
                if st.get("status") != "succeeded" and code == 0:
                    code = -1  # a failed job must never read as success
                prefix = f"{st['reason']}\n" if st.get("reason") else ""
                return code, prefix + "\n".join(tail)
            if time.monotonic() >= deadline:
                msg = f"timed out after {int(timeout_s)}s (deployer job left running)"
                return 124, msg + "\n" + "\n".join(tail)
            time.sleep(2)
    finally:
        stop.set()


def _pr_merged(pr_url: str, repo_slug: str) -> tuple[bool, str | None]:
    pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]
    r = _run(["gh", "pr", "view", pr_number, "-R", repo_slug, "--json", "state,mergedAt"])
    if r.returncode != 0:
        return (False, None)
    import json
    try:
        d = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return (False, None)
    return (d.get("state") == "MERGED", d.get("mergedAt"))


def coxd_identity_email(repo_path: str | None) -> str:
    """coxd's own commit identity email (see gitident). Empty string if none
    resolvable (→ nothing is clean)."""
    return gitident.email(repo_path)


def is_clean_merge(tier: str | None, ci_green: bool, commits: list[tuple[str, str]],
                   bot_emails: list[str], reasons: list[str] | None = None) -> bool:
    """Clean = tier GREEN (or AMBER whose only reason is size), CI green,
    and every commit's PRIMARY author AND committer
    email is coxd's identity (or an allowlisted extra). Co-Authored-By trailers are not
    consulted. Unknown/empty commit list or identity is NOT clean (fail closed)."""
    bots = {b.strip().lower() for b in bot_emails if b and b.strip()}
    tier_ok = tier == "GREEN" or greenlight.is_size_only(tier, reasons)
    if not tier_ok or not ci_green or not commits or not bots:
        return False
    return all((a or "").lower() in bots and (c or "").lower() in bots for a, c in commits)


def _pr_facts(pr_url: str, slug: str) -> tuple[bool, list[tuple[str, str]]]:
    """(ci_green, [(author_email, committer_email)]) for a PR; fail closed on error."""
    import json
    n = pr_url.rstrip("/").rsplit("/", 1)[-1]
    r = _run(["gh", "pr", "view", n, "-R", slug, "--json", "statusCheckRollup"])
    if r.returncode != 0:
        return (False, [])
    try:
        checks = json.loads(r.stdout).get("statusCheckRollup") or []
    except (json.JSONDecodeError, ValueError):
        return (False, [])
    ci_green = bool(checks) and all(
        (c.get("conclusion") or c.get("state")) in ("SUCCESS", "NEUTRAL", "SKIPPED")
        for c in checks)
    r = _run(["gh", "api", "--paginate", f"repos/{slug}/pulls/{n}/commits",
              "--jq", ".[] | [.commit.author.email, .commit.committer.email] | @tsv"])
    if r.returncode != 0:
        return (False, [])
    commits: list[tuple[str, str]] = []
    for line in r.stdout.splitlines():
        a, _, c = line.partition("\t")
        commits.append((a.strip(), c.strip()))
    return (ci_green, commits)


def _record_streak(t: dict, slug: str) -> None:
    """Idempotent per task (store.record_merge dedupes on the task's 'streak' event)."""
    try:
        ci_green, commits = _pr_facts(t["pr_url"], slug)
        bots = [coxd_identity_email(t.get("repo_path"))]
        bots += (registry.load(t["repo"]) or {}).get("bot_authors") or []
        try:
            reasons = json.loads(t.get("greenlight_reasons") or "[]")
        except ValueError:
            reasons = []
        clean = is_clean_merge(t.get("greenlight_tier"), ci_green, commits, bots, reasons)
        store.record_merge(t["repo"], clean, task_id=t["id"])
        _maybe_propose_auto(t["repo"])
    except Exception:  # noqa: BLE001 - bookkeeping must never block cleanup
        pass


def _maybe_propose_auto(repo: str) -> None:
    """Streak reached the threshold → propose `auto` (once) and ping the owner."""
    if store.get_clean_streak(repo) < registry.AUTO_MERGE_STREAK:
        registry.withdraw_proposal(repo)  # streak broke before approval → no longer earned
        return
    if store.propose_auto_merge(repo):
        notify.notify_async(
            "coxd: auto-merge proposed",
            f"{repo}: {registry.AUTO_MERGE_STREAK} clean merges in a row — "
            "open the board and tap 'Approve auto-merge' to enable.", "default")


def _close_issue_if_open(brief: str, repo_slug: str, pr_url: str) -> None:
    m = _ISSUE_URL_RE.search(brief or "")
    if not m or m.group(1) != repo_slug:
        return
    number = m.group(2)
    r = _run(["gh", "issue", "view", number, "-R", repo_slug, "--json", "state"])
    if r.returncode != 0 or '"OPEN"' not in r.stdout:
        return  # already closed (likely auto-linked), or lookup failed — leave it alone
    _run(["gh", "issue", "close", number, "-R", repo_slug,
         "--comment", f"Landed via {pr_url}."])


def _latest_ci_failing(slug: str, branch: str) -> bool:
    """True only if the newest CI run on `branch` has COMPLETED with 'failure'.
    Pending/none/success → not failing, so the deploy proceeds (a merged PR already
    passed its own checks). Non-blocking, best-effort — any lookup error is not-failing."""
    r = _run(["gh", "run", "list", "-R", slug, "--branch", branch, "--limit", "1",
              "--json", "status,conclusion"])
    if r.returncode != 0:
        return False
    import json
    try:
        runs = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return False
    return bool(runs) and runs[0].get("status") == "completed" \
        and runs[0].get("conclusion") == "failure"


def _deploy_stamp(repo_name: str) -> Path:
    return registry.home() / f"last_deploy_{repo_name.replace('/', '_')}.txt"


def _recently_deployed(repo_name: str, sha: str, window_s: int = 180) -> bool:
    """Skip a duplicate deploy of the SAME commit within the window (e.g. a retried hook).
    A different commit always deploys: the deployer builds exactly the sha it is given, so a
    recent deploy of an older commit does not cover a newer merge."""
    p = _deploy_stamp(repo_name)
    try:
        ts, _, prev = p.read_text().partition(" ")
        return prev.strip() == sha and (time.time() - float(ts)) < window_s
    except (ValueError, OSError):
        return False


def _branch_head(slug: str, branch: str) -> str | None:
    """Current head commit of `branch` on GitHub (what a manual 'deploy current main' means)."""
    r = _run(["gh", "api", f"repos/{slug}/commits/{urllib.parse.quote(branch, safe='')}",
              "--jq", ".sha"])
    sha = (r.stdout or "").strip().lower() if r.returncode == 0 else ""
    return sha if _SHA_RE.match(sha) else None


def _pr_merge_sha(pr_url: str, repo_slug: str) -> str | None:
    pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]
    r = _run(["gh", "pr", "view", pr_number, "-R", repo_slug, "--json", "mergeCommit"])
    if r.returncode != 0:
        return None
    try:
        sha = ((json.loads(r.stdout) or {}).get("mergeCommit") or {}).get("oid") or ""
    except (ValueError, AttributeError):
        return None
    sha = sha.lower()
    return sha if _SHA_RE.match(sha) else None


_MIGRATION_RE = re.compile(r"(^|/)(migrations?|alembic|prisma/migrations|db/migrate)(/|$)"
                           r"|\.sql$", re.I)


def _deployed_sha_file(repo_name: str) -> Path:
    return registry.home() / f"deployed_sha_{repo_name.replace('/', '_')}.txt"


def _last_deployed_sha(repo_name: str) -> str | None:
    try:
        sha = _deployed_sha_file(repo_name).read_text().strip().lower()
    except OSError:
        return None
    return sha if _SHA_RE.match(sha) else None


def _record_deployed(repo_name: str, sha: str) -> None:
    try:
        _deployed_sha_file(repo_name).write_text(sha)
    except OSError:
        pass


def _compare(slug: str, base: str, head: str) -> tuple[str, list[str], bool] | None:
    """GitHub compare base...head -> (status, changed files, truncated). None on any error."""
    r = _run(["gh", "api", f"repos/{slug}/compare/{base}...{head}", "--jq",
              "{status: .status, files: [.files[].filename], n: (.files | length)}"])
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
        return str(d["status"]), [str(f) for f in d["files"]], int(d["n"]) >= 300
    except (ValueError, KeyError, TypeError):
        return None


def _migration_hold(slug: str, repo_name: str, sha: str) -> str | None:
    """Why an unattended deploy of `sha` must be held (#53), or None if it may proceed.

    A deploy ships the whole tree at `sha`, so the check covers everything between the last
    successfully deployed commit and `sha` (not just the triggering PR): a migration held
    earlier stays held until a human deploys it. Also holds if `sha` is not strictly ahead of
    the last deployed commit (an older, late-detected merge must not roll production back)."""
    base = _last_deployed_sha(repo_name)
    if base is None:
        return "no deployed-commit baseline yet (deploy once by hand)"
    if base == sha:
        return "this commit is already deployed"
    cmp = _compare(slug, base, sha)
    if cmp is None:
        return "could not compare against the last deployed commit"
    status, files, truncated = cmp
    if status != "ahead":
        return f"merge commit is not ahead of the last deployed commit ({status})"
    if truncated:
        return "too many changed files to check for migrations"
    if any(_MIGRATION_RE.search(f) for f in files):
        return "changes since the last deploy include migrations"
    return None


def run_deploy(t: dict, slug: str, *, manual: bool = False, sha: str | None = None) -> dict:
    """Run the repo's configured deploy command (the ./deploy-to-nas.sh that used to
    follow every merge by hand). Callers:
      - auto (manual=False): `_maybe_deploy`, post-merge, with the merged commit as `sha`.
      - manual (manual=True): the board's Deploy button. A human tapped it, so it
        ignores `enabled` and the coalesce window — but still respects the CI gate
        (never ship on a red target branch).
    `sha` is the commit to deploy; default = the current head of the deploy branch. The
    deployer verifies its source is at exactly this commit (else `stale-source`).
    Returns a result dict for the board; also emits store events + an AFK ping."""
    dep = (registry.load(t["repo"]) or {}).get("deploy") or {}
    if not dep.get("command"):
        return {"skipped": "no deploy command configured for this repo"}
    if manual and not os.environ.get("COXD_DEPLOYER_URL"):
        return {"error": "deployer not configured (COXD_DEPLOYER_URL is unset) — "
                         "cannot deploy from the board"}
    if not manual:
        if not dep.get("enabled"):
            return {"skipped": "auto-deploy disabled for this repo"}
    branch = dep.get("branch", "main")
    sha = (sha or _branch_head(slug, branch) or "").lower()
    if not _SHA_RE.match(sha):
        store.append_event(t["id"], "deploy-skipped",
                           {"reason": f"could not resolve the commit to deploy ({branch})"})
        notify.notify_async("coxd: deploy skipped",
                            f"{slug}: could not resolve the commit to deploy", "high")
        return {"error": "could not resolve the commit to deploy"}
    if not manual and _recently_deployed(t["repo"], sha):
        store.append_event(t["id"], "deploy-coalesced",
                           {"note": "this commit was deployed just now", "sha": sha})
        return {"ok": True, "skipped": "coalesced with a recent deploy of the same commit"}
    if dep.get("gate_on_ci", True) and _latest_ci_failing(slug, branch):
        store.append_event(t["id"], "deploy-skipped", {"reason": f"{branch} CI is red"})
        notify.notify_async("coxd: deploy skipped",
                            f"{slug}: {branch} CI is red — not deploying", "high")
        return {"error": f"{branch} CI is red — not deploying"}
    store.append_event(t["id"], "deploy-start",
                       {"command": dep["command"], "manual": manual, "sha": sha})

    def _claim() -> None:  # only once the deployer accepted the job (never on a 409)
        try:
            _deploy_stamp(t["repo"]).write_text(f"{time.time()} {sha}")
        except OSError:
            pass

    timeout_s = float(dep.get("timeout_s") or DEFAULT_DEPLOY_TIMEOUT_S)
    try:
        rc, out = _stream_deploy(t["id"], t["repo"], timeout_s, sha, on_start=_claim)
    except Exception as e:  # still a loud, terminal event
        rc, out = 127, f"could not start deploy: {e}"
    if rc == 409:  # end state of this attempt: deploy-start -> deploy-skipped (no stamp written)
        store.append_event(t["id"], "deploy-skipped", {"reason": out[-200:]})
        notify.notify_async("coxd: deploy NOT started",
                            f"{slug}: a deploy is already running on the deployer — "
                            "check it (a hung job blocks later deploys)", "high")
        return {"error": "a deploy for this repo is already running on the deployer"}
    if rc == 0:
        _record_deployed(t["repo"], sha)  # baseline for the migration hold range check
        store.append_event(t["id"], "deployed", {"command": dep["command"], "sha": sha})
        notify.notify_async("coxd: deployed",
                            f"{slug}: deployed {sha[:7]} (source verified)", "default")
        return {"ok": True}
    err = out[-400:]
    store.append_event(t["id"], "deploy-failed", {"rc": rc, "err": err})
    notify.notify_async("coxd: deploy FAILED",
                        f"{slug}: deploy rc={rc} — check the board", "high")
    return {"error": f"deploy failed (rc={rc})", "detail": err}


def _hold(t: dict, slug: str, reason: str) -> None:
    store.append_event(t["id"], "deploy-held", {"reason": reason})
    notify.notify_async("coxd: deploy held",
                        f"{slug}: merged, auto-deploy held ({reason}) — deploy by hand",
                        "default")


def _maybe_deploy(t: dict, slug: str) -> None:
    """Post-merge deploy hook for repos that opted in (`deploy.enabled`). Deploys exactly
    the merged commit; the deployer verifies its source matches. HELD (event + ping, nothing
    deployed) when the PR touched migrations (#53: never deploy a migration unattended), when
    that cannot be determined, or when the merge commit cannot be resolved."""
    dep = (registry.load(t["repo"]) or {}).get("deploy") or {}
    if not (dep.get("command") and dep.get("enabled")):
        return
    if not os.environ.get("COXD_DEPLOYER_URL"):
        return _hold(t, slug, "deployer not configured")
    sha = _pr_merge_sha(t["pr_url"], slug) if t.get("pr_url") else None
    if sha is None:
        return _hold(t, slug, "could not resolve the merge commit")
    why = _migration_hold(slug, t["repo"], sha)
    if why:
        return _hold(t, slug, why)
    started = _deploy_bg(t, slug, manual=False, sha=sha)
    if not started:
        _hold(t, slug, "another deploy for this repo is running")


def deploy_task_async(tid: str) -> dict:
    """Kick off a manual deploy for a task's repo in a background thread (a docker
    build takes minutes — don't block the board's event loop). One deploy per repo at
    a time. Returns immediately; progress lands in the task's event feed + an AFK ping."""
    t = store.get_task(tid)
    if not t:
        return {"error": "unknown task"}
    if not t.get("repo_path"):
        return {"error": "task has no repo path"}
    slug = ci_triage.repo_slug(t["repo_path"])
    if not slug:
        return {"error": "cannot resolve the repo slug"}
    if not ((registry.load(t["repo"]) or {}).get("deploy") or {}).get("command"):
        return {"error": "no deploy command configured for this repo"}
    if not os.environ.get("COXD_DEPLOYER_URL"):  # sync, so the board gets a real error
        return {"error": "deployer not configured (COXD_DEPLOYER_URL is unset) — "
                         "cannot deploy from the board"}
    if not _deploy_bg(t, slug, manual=True):
        return {"error": f"{t['repo']} is already deploying"}
    return {"ok": True, "started": True}


def _deploy_bg(t: dict, slug: str, *, manual: bool, sha: str | None = None) -> bool:
    """Run run_deploy in a thread, one per repo. False if this repo is already deploying."""
    tid = t["id"]
    with _deploy_lock:
        if t["repo"] in _deploy_inflight:
            return False
        _deploy_inflight.add(t["repo"])

    def _bg() -> None:
        try:
            run_deploy(t, slug, manual=manual, sha=sha)
        except Exception as e:  # never let the thread die without a terminal event
            print(f"[deploy {tid}] crashed: {e!r}", flush=True)
            try:
                store.append_event(tid, "deploy-failed",
                                   {"rc": -1, "err": f"coxd error: {e!r}"[-400:]})
                notify.notify_async("coxd: deploy FAILED",
                                    f"{slug}: coxd error — {e!r}"[:200], "high")
            except Exception:
                pass
        finally:
            with _deploy_lock:
                _deploy_inflight.discard(t["repo"])

    threading.Thread(target=_bg, daemon=True).start()
    return True


def check_and_cleanup_merged() -> None:
    """One pass: for every pr_ready task, check if its PR merged; if so, clean up."""
    for t in store.list_tasks():
        if t["state"] != "pr_ready" or not t["pr_url"] or not t["repo_path"]:
            continue
        slug = ci_triage.repo_slug(t["repo_path"])
        if not slug:
            continue
        merged, _ = _pr_merged(t["pr_url"], slug)
        if not merged:
            continue
        store.append_event(t["id"], "auto-cleanup", {"pr_url": t["pr_url"]})
        _close_issue_if_open(t["brief"], slug, t["pr_url"])
        _record_streak(t, slug)
        branch = f"coxd/{t['id']}"
        worktree.remove(Path(t["repo_path"]), Path(t["worktree"]), branch)
        _run(["git", "push", "origin", "--delete", branch], cwd=t["repo_path"])
        pr_number = t["pr_url"].rstrip("/").rsplit("/", 1)[-1]
        store.set_state(t["id"], "landed", f"merged as PR #{pr_number}")
        try:
            import intake
            intake.clear_working(t["repo"], slug, t["brief"])
        except Exception:  # noqa: BLE001 - label hygiene must never block cleanup
            pass
        _maybe_deploy(t, slug)
        try:
            import rebase
            rebase.mark_merged(t["repo"])
        except Exception:  # noqa: BLE001 - rebasing others must never block cleanup
            pass

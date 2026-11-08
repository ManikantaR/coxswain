"""Post-merge cleanup poller (DESIGN-V35 extension).

Every PR merge tonight needed the same three manual steps afterward: remove the
worktree, delete the local+remote coxd/<id> branch, close the issue if it didn't
auto-link. None of that requires human judgment — it's pure bookkeeping once a
human has already made the one real decision (the merge itself). Detect the merge
and do the bookkeeping automatically instead of repeating it by hand every time.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
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


def _stream_deploy(tid: str, command: str, cwd: str, timeout_s: float) -> tuple[int, str]:
    """Run the deploy command, STREAMING its output instead of capturing it to the end.

    Why (the "Deploy button dies silently" bug): the old path was
    subprocess.run(capture_output=True) with no timeout — a multi-minute docker build
    produced zero visible output, a hung step blocked the thread forever, and nothing
    ever reached `docker logs`. Now every line is echoed (flushed) to coxd's stdout so
    `docker logs coxd` shows the build live, a `deploy-progress` event carries the
    latest line to the board every ~20s, and a watchdog kills the whole process group
    after `timeout_s`. Returns (returncode, tail-of-output); rc=124 on timeout."""
    p = subprocess.Popen(
        ["bash", "-lc", command], cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        start_new_session=True,  # own process group → the watchdog can kill children too
    )
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    watchdog = threading.Timer(timeout_s, _kill)
    watchdog.daemon = True
    watchdog.start()
    tail: deque[str] = deque(maxlen=40)
    last_emit = time.monotonic()
    try:
        assert p.stdout is not None
        for line in p.stdout:
            line = line.rstrip("\n")
            tail.append(line)
            print(f"[deploy {tid}] {line}", flush=True)
            if time.monotonic() - last_emit >= _PROGRESS_EVERY_S:
                store.append_event(tid, "deploy-progress", {"line": line[-200:]})
                last_emit = time.monotonic()
        rc = p.wait()
    finally:
        watchdog.cancel()
    out = "\n".join(tail)
    if timed_out.is_set():
        return 124, f"timed out after {int(timeout_s)}s (killed)\n{out}"
    return rc, out


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


def _recently_deployed(repo_name: str, window_s: int = 180) -> bool:
    """Coalesce a burst of merges (a whole backlog landing) into ONE deploy: skip if
    this repo deployed within the window. Deploys are idempotent (they push latest
    main), so one covers the batch — avoids N deploys for an N-issue batch."""
    p = _deploy_stamp(repo_name)
    try:
        return p.exists() and (time.time() - float(p.read_text())) < window_s
    except (ValueError, OSError):
        return False


def run_deploy(t: dict, slug: str, *, manual: bool = False) -> dict:
    """Run the repo's configured deploy command (the ./deploy-to-nas.sh that used to
    follow every merge by hand). Two callers:
      - auto (manual=False): post-merge, opt-in per repo (`deploy.enabled`) and
        coalesced so a batch of merges deploys once.
      - manual (manual=True): the board's Deploy button. A human tapped it, so it
        ignores `enabled` and the coalesce window — but still respects the CI gate
        (never ship on a red target branch).
    Returns a result dict for the board; also emits store events + an AFK ping."""
    dep = (registry.load(t["repo"]) or {}).get("deploy") or {}
    if not dep.get("command"):
        return {"skipped": "no deploy command configured for this repo"}
    if not manual:
        if not dep.get("enabled"):
            return {"skipped": "auto-deploy disabled for this repo"}
        if _recently_deployed(t["repo"]):
            store.append_event(t["id"], "deploy-coalesced",
                               {"note": "another deploy for this repo ran just now"})
            return {"ok": True, "skipped": "coalesced with a recent deploy"}
    branch = dep.get("branch", "main")
    if dep.get("gate_on_ci", True) and _latest_ci_failing(slug, branch):
        store.append_event(t["id"], "deploy-skipped", {"reason": f"{branch} CI is red"})
        notify.notify_async("coxd: deploy skipped",
                            f"{slug}: {branch} CI is red — not deploying", "high")
        return {"error": f"{branch} CI is red — not deploying"}
    cwd = dep.get("cwd") or t["repo_path"]
    # Never ship stale code: fast-forward the deploy checkout to the target branch
    # first. ff-only so a diverged/dirty clone fails loud instead of silently merging.
    pr = _run(["git", "-C", cwd, "pull", "--ff-only", "origin", branch])
    if pr.returncode != 0:
        store.append_event(t["id"], "deploy-pull-failed",
                           {"branch": branch, "err": (pr.stderr or pr.stdout or "")[-300:]})
        return {"error": f"git pull --ff-only origin {branch} failed — "
                         "refusing to deploy stale code",
                "detail": (pr.stderr or pr.stdout or "")[-300:]}
    store.append_event(t["id"], "deploy-start", {"command": dep["command"], "manual": manual})
    try:
        _deploy_stamp(t["repo"]).write_text(str(time.time()))  # claim before running → coalesce
    except OSError:
        pass
    timeout_s = float(dep.get("timeout_s") or DEFAULT_DEPLOY_TIMEOUT_S)
    try:
        rc, out = _stream_deploy(t["id"], dep["command"], cwd, timeout_s)
    except OSError as e:  # bash/cwd missing → still a loud, terminal event
        rc, out = 127, f"could not start deploy: {e}"
    if rc == 0:
        store.append_event(t["id"], "deployed", {"command": dep["command"]})
        notify.notify_async("coxd: deployed",
                            f"{slug}: deploy ✓" + ("" if manual else " after merge"), "default")
        return {"ok": True}
    err = out[-400:]
    store.append_event(t["id"], "deploy-failed", {"rc": rc, "err": err})
    notify.notify_async("coxd: deploy FAILED",
                        f"{slug}: deploy rc={rc} — check the board", "high")
    return {"error": f"deploy failed (rc={rc})", "detail": err}


def _maybe_deploy(t: dict, slug: str) -> None:
    """Auto post-merge deploy (opt-in per repo). Thin wrapper over run_deploy."""
    run_deploy(t, slug, manual=False)


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
    with _deploy_lock:
        if t["repo"] in _deploy_inflight:
            return {"error": f"{t['repo']} is already deploying"}
        _deploy_inflight.add(t["repo"])

    def _bg() -> None:
        try:
            run_deploy(t, slug, manual=True)
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
    return {"ok": True, "started": True}


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

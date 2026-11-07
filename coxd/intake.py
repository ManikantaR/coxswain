"""Label-driven intake: an open issue labelled `coxd-ready` becomes a coxd task.

Polled from the supervisor (see `run`). Per repo it is opt-in (`intake: true` in the
registry entry; default off). Merge stays human. Safety notes:
  - the label must have been added by a human with write/maintain/admin (checked via
    the permissions API - never the issue author);
  - title/body are stripped of invisible/bidi characters and PII-scanned before they
    reach a brief; a hit adds a label only - never a comment, and the matched text and
    rule detail never leave the machine (log: rule + location only).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time

import board
import ci_triage
import dispatch
import factory
import pii
import registry
import repos
import store

READY, WORKING, WAITING = "coxd-ready", "coxd-working", "coxd-waiting"
UNTRUSTED, PII_BLOCK = "coxd-untrusted", "coxd-pii-block"
LABELS = {
    READY: ("Queued for coxd pickup", "0e8a16"),
    WORKING: ("coxd is working on this", "1d76db"),
    WAITING: ("Waiting on a dependency issue", "fbca04"),
    UNTRUSTED: ("coxd-ready was added by an untrusted user", "b60205"),
    PII_BLOCK: ("coxd blocked: possible sensitive data", "b60205"),
}
MAX_PER_REPO = 3
_TRUSTED_PERMS = {"write", "maintain", "admin"}
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]|\[bot\])*$")
_INVISIBLE_RE = re.compile("[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e"
                           "\u2060-\u206f\ufeff]")
_DEP_RE = re.compile(
    r"^[ \t>*-]*depends\s+on\s+(?:#(\d+)|https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+))\b",
    re.IGNORECASE | re.MULTILINE)

_status: dict = {"at": None, "taken": 0}
_labels_ready: set[str] = set()


def interval_s() -> float:
    try:
        return max(1.0, float(os.environ.get("COXD_INTAKE_INTERVAL_S", "300")))
    except ValueError:
        return 300.0


def status() -> dict:
    return dict(_status)


def _gh(args: list[str]) -> tuple[int, str]:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout


def _json(args: list[str]):
    rc, out = _gh(args)
    if rc != 0:
        raise RuntimeError(f"gh {args[0]} failed")
    return json.loads(out or "null")


def clean(text: str) -> str:
    return _INVISIBLE_RE.sub("", text or "")


def _relabel(slug: str, number: int, add: str | None = None,
             remove: str | None = None) -> None:
    args = ["issue", "edit", str(number), "-R", slug]
    if remove:
        args += ["--remove-label", remove]
    if add:
        args += ["--add-label", add]
    rc, _out = _gh(args)
    if rc != 0:
        raise RuntimeError("label update failed")


def _ensure_labels(slug: str) -> None:
    if slug in _labels_ready:
        return
    have = {x.get("name") for x in _json(
        ["label", "list", "-R", slug, "--json", "name", "--limit", "200"]) or []}
    for name, (desc, color) in LABELS.items():
        if name not in have:
            _gh(["label", "create", name, "-R", slug, "--description", desc,
                 "--color", color])
    _labels_ready.add(slug)


def _labeller(slug: str, number: int) -> tuple[dict | None, str]:
    """(actor, created_at) of the most recent `labeled coxd-ready` timeline event."""
    pages = _json(["api", f"repos/{slug}/issues/{number}/timeline",
                   "--paginate", "--slurp"]) or []
    events = [e for p in pages for e in (p if isinstance(p, list) else [p])]
    last = None
    for e in events:
        if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == READY:
            last = e
    last = last or {}
    return last.get("actor") or None, str(last.get("created_at") or "")


_EDIT_Q = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){"
           "issue(number:$n){lastEditedAt userContentEdits(first:1){nodes{"
           "editedAt editor{login __typename}}}}}}")


def _edited_after(slug: str, number: int, label_at: str) -> dict | None:
    """The editor of the issue title/body if it was edited after the label was added
    (a maintainer approved the text they saw, not a later rewrite). {} if the editor
    is unknown. None if not edited since. Raises if the lookup fails."""
    owner, repo = slug.split("/", 1)
    d = _json(["api", "graphql", "-f", f"query={_EDIT_Q}", "-f", f"o={owner}",
               "-f", f"r={repo}", "-F", f"n={number}"]) or {}
    issue = (((d.get("data") or {}).get("repository") or {}).get("issue")) or {}
    edited = issue.get("lastEditedAt")
    if not edited or not label_at or edited <= label_at:  # ISO-8601 UTC sorts lexically
        return None
    nodes = (issue.get("userContentEdits") or {}).get("nodes") or []
    ed = (nodes[0].get("editor") if nodes else None) or {}
    return {"login": ed.get("login"),
            "type": "User" if ed.get("__typename") == "User" else ed.get("__typename")}


def _is_trusted(slug: str, actor: dict | None) -> bool | None:
    """True/False, or None when the permission lookup itself failed (retry later)."""
    if not actor:
        return False
    login = str(actor.get("login") or "")
    if (actor.get("type") != "User" or login.endswith("[bot]")
            or not _LOGIN_RE.match(login)):
        return False
    try:
        d = _json(["api", f"repos/{slug}/collaborators/{login}/permission"]) or {}
    except Exception:  # noqa: BLE001
        return None
    return d.get("permission") in _TRUSTED_PERMS or d.get("role_name") in _TRUSTED_PERMS


def _open_deps(slug: str, body: str) -> bool:
    """True while any `Depends on` issue is open (or cannot be looked up)."""
    for m in _DEP_RE.finditer(body):
        dep_slug, num = (slug, m.group(1)) if m.group(1) else (m.group(2), m.group(3))
        try:
            state = (_json(["issue", "view", num, "-R", dep_slug,
                            "--json", "state"]) or {}).get("state")
        except Exception:  # noqa: BLE001
            return True
        if state != "CLOSED":
            return True
    return False


def _has_task(url: str) -> bool:
    pat = re.compile(r"^GitHub issue: " + re.escape(url) + r"\s*$", re.MULTILINE)
    return any(pat.search(t.get("brief") or "") for t in store.list_tasks()
               if not board._is_archived(t))


def _process_repo(name: str, path: str) -> int:
    slug = ci_triage.repo_slug(path)
    if not slug:
        return 0
    issues = _json(["issue", "list", "-R", slug, "--label", READY, "--state", "open",
                    "--json", "number,title,body,url,createdAt,labels",
                    "--limit", "100"]) or []
    if not issues:
        return 0
    _ensure_labels(slug)
    deny, _missing = pii.load_denylist()
    taken = 0
    for it in sorted(issues, key=lambda i: i.get("createdAt") or ""):
        if taken >= MAX_PER_REPO:
            break
        n, url = int(it["number"]), it["url"]
        try:
            if _has_task(url):
                continue
            actor, label_at = _labeller(slug, n)
            trusted = _is_trusted(slug, actor)
            if trusted is None:
                continue
            if not trusted:
                _relabel(slug, n, add=UNTRUSTED, remove=READY)
                print(f"intake: {name}#{n} untrusted label - blocked")
                continue
            editor = _edited_after(slug, n, label_at)
            if editor is not None:
                ok = _is_trusted(slug, editor) if editor.get("login") else False
                if ok is None:
                    continue
                if not ok:
                    _relabel(slug, n, add=UNTRUSTED, remove=READY)
                    print(f"intake: {name}#{n} edited after labelling - blocked")
                    continue
            title, body = clean(it.get("title")), clean(it.get("body"))
            if _open_deps(slug, body):
                if WAITING not in {lb.get("name") for lb in it.get("labels") or []}:
                    _relabel(slug, n, add=WAITING)
                continue
            hits = pii.scan({"title": title, "body": body}, deny)
            if hits:
                _relabel(slug, n, add=PII_BLOCK, remove=READY)
                print(f"intake: {name}#{n} pii-block "
                      + ", ".join(f"{h.rule}@{h.location}" for h in hits))
                continue
            dispatch.dispatch(path, f"{title}\n\n{body}\n\nGitHub issue: {url}".strip())
            taken += 1
            _relabel(slug, n, add=WORKING, remove=READY)
            if WAITING in {lb.get("name") for lb in it.get("labels") or []}:
                _relabel(slug, n, remove=WAITING)
        except Exception as e:  # noqa: BLE001 - one bad issue must not stop the poll
            print(f"intake: {name}#{n} failed: {type(e).__name__}")
    return taken


def enabled_repos() -> list[dict]:
    return [r for r in repos.list_repos()
            if (registry.load(r["name"]) or {}).get("intake") is True]


def poll_once() -> int | None:
    """One pass over opted-in repos. None (and zero calls) while the factory is paused."""
    if factory.is_paused():
        return None
    taken = 0
    for r in enabled_repos():
        try:
            taken += _process_repo(r["name"], r["path"])
        except Exception as e:  # noqa: BLE001
            print(f"intake: {r['name']} poll failed: {type(e).__name__}")
    _status.update(at=time.time(), taken=taken)
    return taken


def clear_working(repo_name: str, slug: str, brief: str) -> None:
    """A task landed: drop `coxd-working` from its issue (intake-enabled repos only)."""
    if (registry.load(repo_name) or {}).get("intake") is not True:
        return
    m = re.search(r"^GitHub issue: https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)\s*$",
                  brief or "", re.MULTILINE)
    if m and m.group(1) == slug:
        _relabel(slug, int(m.group(2)), remove=WORKING)


async def run() -> None:
    while True:
        try:
            await asyncio.to_thread(poll_once)
        except Exception as e:  # noqa: BLE001
            print(f"intake pass failed: {type(e).__name__}")
        await asyncio.sleep(interval_s())

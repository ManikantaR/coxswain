"""Push + open PR (DESIGN-V35; survives the pivot).

The control plane pushes and opens the PR — workers never can (the no-push hook).
If the repo has no GitHub remote (a local/scratch repo), this is a no-op and the
task is simply pr_ready-local. Never raises into the loop; a push/PR failure
becomes a typed needs-human reason the caller can route.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import gate
import greenlight
import pii
import registry
import sandbox
import store

_TEST_PATH_MARKERS = ("__tests__/", ".spec.ts", ".spec.tsx", ".test.ts", ".test.tsx")


def _has_gh_remote(repo_path: Path) -> bool:
    r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_path,
                       capture_output=True, text=True)
    return r.returncode == 0 and ("github.com" in r.stdout or "git@" in r.stdout)


def _diff_stat_lines(wt: Path, base: str) -> list[str]:
    r = sandbox.run_in_worktree(["git", "diff", "--stat", f"{base}...HEAD"], wt)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _new_test_count(wt: Path, base: str) -> int:
    """Heuristic count of NEW test cases: added lines opening an `it(`/`test(` block
    in a touched test file. Not exact (doesn't catch every runner's syntax, e.g.
    pytest), but cheap, honest, and right for this (TS/JS) codebase — a count, never
    a coverage-% claim we can't actually compute here."""
    r = sandbox.run_in_worktree(
        ["git", "diff", f"{base}...HEAD", "--",
         "*.spec.ts", "*.spec.tsx", "*.test.ts", "*.test.tsx", "**/__tests__/**"], wt)
    pattern = re.compile(r"^\+\s*(it|test)(\.\w+)?\(")
    return sum(1 for ln in r.stdout.splitlines() if pattern.match(ln))


def _test_evidence(wt: Path) -> str:
    """Cheap, honest proxy for 'were tests written' — a diff-stat of test files
    (never a claim about coverage %, which we can't compute without a coverage
    run). If no test files changed at all, say so plainly rather than omit it."""
    base = gate._base_ref(wt)
    all_lines = _diff_stat_lines(wt, base)
    test_lines = [ln for ln in all_lines if any(m in ln for m in _TEST_PATH_MARKERS)]
    if not test_lines:
        return "_No test files were added or modified in this change._"
    new_count = _new_test_count(wt, base)
    header = f"**{new_count} new test case(s) added** (heuristic count):\n" if new_count else ""
    return header + "```\n" + "\n".join(test_lines) + "\n```"


def _gate_checklist(gate_steps: dict) -> str:
    if not gate_steps:
        return "_Gate results unavailable._"
    icon = {"ok": "✅", "red": "❌", "skip": "⏭️ skipped", "none": "n/a (no check configured)"}
    return "\n".join(f"- {icon.get(v, v)} `{k}`" for k, v in gate_steps.items())


def _review_section(verdict: str | None, findings: list[dict]) -> str:
    if not verdict:
        return ""
    lines = [f"**Review verdict:** {verdict}"]
    if findings:
        lines.append("\nFindings (advisory — did not block landing):")
        for f in findings:
            loc = f"{f.get('file', '')}:{f.get('line', '')}".strip(":")
            lines.append(f"- [{f.get('severity', '?')}/{f.get('pillar', '?')}]"
                         f"{f' {loc} —' if loc else ''} {f.get('summary', '')}")
    return "\n".join(lines)


_SUMMARY_MAX_CHARS = 2000


def _diff_stat(wt: Path, base: str = "origin/main") -> tuple[list[str], int, int]:
    """Files changed + total +/- for the branch vs base — the greenlight diff-size
    input. Best-effort; a git failure yields an empty stat."""
    r = sandbox.run_in_worktree(["git", "diff", "--numstat", f"{base}...HEAD"], wt)
    files: list[str] = []
    added = deleted = 0
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                a, d, path = parts
                files.append(path)
                added += int(a) if a.isdigit() else 0
                deleted += int(d) if d.isdigit() else 0
    return files, added, deleted


def _greenlight(t: dict, wt: Path) -> tuple[str, str]:
    """(tier, header) for this PR from coxd's own signal (gate green by construction,
    review verdict/findings, diff size, per-repo sacred_globs). Advisory — a failure
    here never blocks the ship."""
    try:
        entry = registry.load(t["repo"]) or {}
        files, added, deleted = _diff_stat(wt, f"origin/{entry.get('target_branch', 'main')}")
        findings = json.loads(t.get("review_findings") or "[]")
        tier, reasons = greenlight.tier(findings, t.get("review_verdict"),
                                        files, added, deleted, entry.get("sacred_globs") or [])
        return tier, greenlight.header(tier, reasons, findings)
    except Exception:  # noqa: BLE001 — greenlight is advisory, never fail a ship over it
        return "", ""


def _build_body(t: dict, wt: Path, gl_header: str = "") -> str:
    summary = t.get("summary") or "_(worker produced no summary text)_"
    if len(summary) > _SUMMARY_MAX_CHARS:
        summary = summary[:_SUMMARY_MAX_CHARS].rstrip() + "\n\n_(summary truncated)_"
    gate_steps = json.loads(t.get("gate_steps") or "{}")
    verdict = t.get("review_verdict")
    findings = json.loads(t.get("review_findings") or "[]")

    sections = [
        *([gl_header, "\n---"] if gl_header else []),
        summary,
        "\n---\n### Test evidence",
        _test_evidence(wt),
        "\n### Gate results",
        _gate_checklist(gate_steps),
    ]
    test_output = t.get("test_output")
    if test_output:
        sections += ["\n<details><summary>Test run output (tail)</summary>\n\n```\n"
                     + test_output + "\n```\n</details>"]
    review = _review_section(verdict, findings)
    if review:
        sections += ["\n### Review", review]
    sections += [f"\n---\nDispatched via coxd (`{t['id']}`) · cost ${t['cost'] or 0:.3f} · 🤖 coxd"]
    return "\n".join(sections)


def _pr_title(t: dict) -> str:
    """PR title from the captain's raw intent — never the prepended rules block.
    Older tasks predate the `intent` column; fall back to stripping a leading
    rules block off `brief`. Trailing 'GitHub issue: <url>' is dropped so the
    title stays the intent, not a URL."""
    src = t.get("intent") or t.get("brief") or ""
    lines = [ln for ln in src.strip().splitlines() if ln.strip()]
    # Skip a leading learned-rules block if this is a legacy row without intent.
    if lines and lines[0].lstrip().startswith("## Learned rules"):
        lines = [ln for ln in lines[1:] if not ln.lstrip().startswith("- ")]
    first = (lines[0] if lines else "coxd task").strip()
    first = re.split(r"\s*GitHub issue:\s*", first, maxsplit=1)[0].strip() or first
    return first[:70]


_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _added_lines(diff: str) -> dict[str, str]:
    """Added lines only of unified-diff text, keyed by file path. Hunk-aware: line
    counts from each `@@` header decide what is content, so an added line that merely
    looks like a `+++ ` header (or `--- `) is still scanned. Splits on newline only."""
    out: dict[str, list[str]] = {}
    cur: str | None = None
    old = new = 0
    for ln in diff.split("\n"):
        if old > 0 or new > 0:  # inside a hunk body
            if ln.startswith("+"):
                new -= 1
                if cur is not None:
                    out[cur].append(ln[1:])
            elif ln.startswith("-"):
                old -= 1
            elif ln.startswith("\\"):
                pass  # "\ No newline at end of file"
            else:
                old -= 1
                new -= 1
            continue
        if ln.startswith("\x01") or ln.startswith("diff --git"):
            cur = None
        elif ln.startswith("+++ "):
            p = ln[4:].strip()
            if len(p) > 1 and p.startswith('"') and p.endswith('"'):
                p = p[1:-1]  # quoted (tab/quote/backslash); quotePath=false handles UTF-8
            if p == "/dev/null":
                cur = None
            else:
                cur = p[2:] if p.startswith("b/") else p
                out.setdefault(cur, [])
        else:
            m = _HUNK_RE.match(ln)
            if m:
                old = int(m.group(1)) if m.group(1) is not None else 1
                new = int(m.group(2)) if m.group(2) is not None else 1
    return {k: "\n".join(v) for k, v in out.items()}


def _git_text(repo_path: Path, *args: str) -> str:
    """Run git in the MAIN checkout (never the worker-owned worktree); raise on failure.
    Output decoded with errors='replace' so non-UTF-8 bytes can't crash the scan."""
    r = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=repo_path,
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed")
    return r.stdout.decode("utf-8", errors="replace")


_PATCH_FLAGS = ("--no-color", "-U0", "--text", "--no-ext-diff", "--no-textconv")


def _resolve_base(repo_path: Path, preferred: str | None = None) -> str:
    """The base the worktree was branched from: the registry's explicit target branch,
    else origin/HEAD, origin/main, origin/master. Raises (fail closed) if none resolves."""
    cands = ([f"origin/{preferred}"] if preferred else []) + [
        "origin/HEAD", "origin/main", "origin/master"]
    for ref in cands:
        r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                           cwd=repo_path, capture_output=True)
        if r.returncode == 0:
            return ref
    raise RuntimeError("no base ref resolves")


def _pii_items(repo_path: Path, base: str, ref: str, title: str, body: str,
               deny: list[str] | None = None) -> dict[str, str]:
    """What publishing `ref` would expose, as scan items: each commit's own patch
    (add-then-remove history; merges diffed against the first parent), the net diff,
    changed file paths, every commit message, the PR title and body. Author/committer
    names are NOT scanned. Changed paths are scanned too; a path that itself hits is
    reported only as `path:<i>` / `diff:path:<i>`, never by name."""
    _git_text(repo_path, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    _git_text(repo_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    flags = (*_PATCH_FLAGS, "--diff-merges=first-parent")
    patches = _git_text(repo_path, "log", "-p", "--format=%x01%H", *flags, f"{base}..{ref}")
    net = _git_text(repo_path, "diff", *_PATCH_FLAGS, f"{base}...{ref}")
    names = (_git_text(repo_path, "log", "--name-only", "-z", "--format=",
                       "--diff-merges=first-parent", f"{base}..{ref}")
             + "\0" + _git_text(repo_path, "diff", "--name-only", "-z", f"{base}...{ref}"))
    paths = sorted({x for x in names.split("\0") if x})
    msgs = _git_text(repo_path, "log", "--format=%B%x00", f"{base}..{ref}")
    idx = {pth: i for i, pth in enumerate(paths)}
    dirty = {pth for pth in paths if pii.scan({"p": pth}, deny or [])}
    items: dict[str, str] = {}
    for pth in paths:
        items[f"path:{idx[pth]}"] = pth
    for src in (patches, net):
        for pth, txt in _added_lines(src).items():
            loc = f"diff:path:{idx[pth]}" if pth in dirty and pth in idx else f"diff:{pth}"
            items[loc] = items.get(loc, "") + "\n" + txt
    for i, msg in enumerate(x for x in msgs.split("\0") if x.strip()):
        items[f"commit:{i}"] = msg
    items["pr-title"] = title
    items["pr-body"] = body
    return items


def _pii_block(task_id: str, reason: str, data: dict) -> str:
    store.append_event(task_id, "pii-block", {"reason": reason, **data})
    return reason


def _pii_gate(task_id: str, t: dict, repo_path: Path, branch: str, wt: Path,
              ctx: dict | None = None) -> str | None:
    """None if clean, else the block reason ('pii-block' | 'pii-scan-error').
    Fails closed on ANY error, including building the title/body."""
    try:
        title = _pr_title(t)
        _tier, gl_header = _greenlight(t, wt)
        body = _build_body(t, wt, gl_header)
        # Pin the exact commit: scan this sha and push this sha (no check-then-use gap).
        sha = _git_text(repo_path, "rev-parse", "--verify",
                        f"refs/heads/{branch}^{{commit}}").strip()
        if ctx is not None:
            ctx.update(sha=sha, title=title, body=body, tier=_tier)
        entry = registry.load(t["repo"]) or {}
        base = _resolve_base(repo_path, entry.get("target_branch"))
        deny, missing = pii.load_denylist()
        if missing:
            store.append_event(task_id, "pii-denylist-missing", {})
        hits = pii.scan(_pii_items(repo_path, base, sha, title, body, deny),
                        deny)
        allow = pii.load_allow()
        kept = []
        for h in hits:  # `secret` is never overridable (load_allow drops it too)
            if h.rule != "secret" and (h.rule, h.location, task_id) in allow:
                store.append_event(task_id, "pii-allowed", {"rule": h.rule, "location": h.location})
            else:
                kept.append(h)
        hits = kept
    except Exception as e:  # noqa: BLE001 — fail closed
        return _pii_block(task_id, "pii-scan-error", {"error": type(e).__name__})
    if hits:
        return _pii_block(task_id, "pii-block", {
            "hits": [{"rule": h.rule, "location": h.location} for h in hits]})
    return None


def ship(task_id: str) -> tuple[str, str | None]:
    """Returns (outcome, pr_url). outcome: 'pr' | 'local' | 'push-error' | 'pr-error'
    | 'pii-block' | 'pii-scan-error'."""
    t = store.get_task(task_id)
    wt = Path(t["worktree"])
    repo_path = Path(t["repo_path"]) if t["repo_path"] else None
    branch = f"coxd/{task_id}"

    if repo_path is None or not _has_gh_remote(repo_path):
        return ("local", None)  # nothing to push to — the work is on the local branch

    ctx: dict = {}
    reason = _pii_gate(task_id, t, repo_path, branch, wt, ctx)
    if reason:  # loop.py owns the state transition (needs_human / <reason>)
        return (reason, None)

    # From the main checkout, never from the worker-owned worktree (see sandbox.py):
    # the branch ref is shared, and root's config/hooks there are not worker-writable.
    sha = ctx.get("sha")
    refspec = [f"{sha}:refs/heads/{branch}"] if sha else [branch]
    push = subprocess.run(["git", "push", "origin", *refspec], cwd=repo_path,
                          capture_output=True, text=True)
    if push.returncode != 0:
        store.append_event(task_id, "ship-error", {"stage": "push", "err": push.stderr[-300:]})
        return ("push-error", None)

    if t.get("pr_url"):
        # A follow-up round on a task whose PR is already open (human resume from
        # pr_ready): the push above updated that PR; never open a second one.
        return ("pr", t["pr_url"])

    if ctx:  # reuse exactly what was scanned
        title, body, tier = ctx["title"], ctx["body"], ctx["tier"]
    else:
        title = _pr_title(t)
        tier, gl_header = _greenlight(t, wt)
        body = _build_body(t, wt, gl_header)
    pr = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body, "--head", branch],
        cwd=repo_path, capture_output=True, text=True)
    if pr.returncode != 0:
        store.append_event(task_id, "ship-error", {"stage": "pr", "err": pr.stderr[-300:]})
        return ("pr-error", None)
    url = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else None
    if url:
        store.set_pr_url(task_id, url)
        if tier:
            store.append_event(task_id, "greenlight", {"tier": tier})
        if tier == "GREEN":  # pre-label low-risk PRs for one-click merge (best-effort)
            subprocess.run(["gh", "label", "create", "coxd-greenlight", "--color", "0E8A16",
                            "--description", "coxd: low-risk, safe to one-click merge", "--force"],
                           cwd=repo_path, capture_output=True, text=True)
            subprocess.run(["gh", "pr", "edit", url, "--add-label", "coxd-greenlight"],
                           cwd=repo_path, capture_output=True, text=True)
    return ("pr", url)

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
import registry
import store

_TEST_PATH_MARKERS = ("__tests__/", ".spec.ts", ".spec.tsx", ".test.ts", ".test.tsx")


def _has_gh_remote(repo_path: Path) -> bool:
    r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_path,
                       capture_output=True, text=True)
    return r.returncode == 0 and ("github.com" in r.stdout or "git@" in r.stdout)


def _diff_stat_lines(wt: Path, base: str) -> list[str]:
    r = subprocess.run(["git", "diff", "--stat", f"{base}...HEAD"], cwd=wt,
                       capture_output=True, text=True)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _new_test_count(wt: Path, base: str) -> int:
    """Heuristic count of NEW test cases: added lines opening an `it(`/`test(` block
    in a touched test file. Not exact (doesn't catch every runner's syntax, e.g.
    pytest), but cheap, honest, and right for this (TS/JS) codebase — a count, never
    a coverage-% claim we can't actually compute here."""
    r = subprocess.run(
        ["git", "diff", f"{base}...HEAD", "--",
         "*.spec.ts", "*.spec.tsx", "*.test.ts", "*.test.tsx", "**/__tests__/**"],
        cwd=wt, capture_output=True, text=True,
    )
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
    r = subprocess.run(["git", "diff", "--numstat", f"{base}...HEAD"],
                       cwd=wt, capture_output=True, text=True)
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


def ship(task_id: str) -> tuple[str, str | None]:
    """Returns (outcome, pr_url). outcome: 'pr' | 'local' | 'push-error' | 'pr-error'."""
    t = store.get_task(task_id)
    wt = Path(t["worktree"])
    repo_path = Path(t["repo_path"]) if t["repo_path"] else None
    branch = f"coxd/{task_id}"

    if repo_path is None or not _has_gh_remote(repo_path):
        return ("local", None)  # nothing to push to — the work is on the local branch

    push = subprocess.run(["git", "push", "-u", "origin", branch], cwd=wt,
                          capture_output=True, text=True)
    if push.returncode != 0:
        store.append_event(task_id, "ship-error", {"stage": "push", "err": push.stderr[-300:]})
        return ("push-error", None)

    title = _pr_title(t)
    tier, gl_header = _greenlight(t, wt)
    body = _build_body(t, wt, gl_header)
    pr = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body, "--head", branch],
        cwd=wt, capture_output=True, text=True)
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
                           cwd=wt, capture_output=True, text=True)
            subprocess.run(["gh", "pr", "edit", url, "--add-label", "coxd-greenlight"],
                           cwd=wt, capture_output=True, text=True)
    return ("pr", url)

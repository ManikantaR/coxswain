"""Session-mining self-learning (issue #7): mine ~/.claude session transcripts for
candidate compounding rules, across EVERY project on this machine — not just
coxd's own task DB. The mistakes this is meant to catch (a Postgres ANY() bug
fixed once then hit again 6 times, a migration silently skipped for hours, the
same test flake fixed twice on two branches) surfaced in whichever repo's
session hit them, never in coxd's own store.

Two modes, both writing to store.rule_suggestions with status='pending' — never
auto-applied, the board (board.py) is the only path to `rules.add_rule`:

  --since  (lightweight/periodic): cheap textual signals only — no LLM call.
           Scans transcript files modified since the last run's high-water
           mark for user-correction phrases ("no, don't", "that's wrong",
           "revert that") and repeated identical error strings across turns.
  --deep   (on-demand): a FRESH-CONTEXT Claude review pass over the full
           transcript(s) since the last deep pass. Fresh context matters
           (Osmani's point): a continued session rationalizes its own past
           mistakes, a cold read judges it honestly. Extracts concrete,
           durable, one-line rules + the repo they apply to + a quote,
           machine-proposed equivalent of cox/server.py's old manual
           `promote_rule`.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import time
from pathlib import Path

import registry
import store

# Cheap textual signals a human corrected the model — no LLM call, just regexes.
_CORRECTION_RE = re.compile(
    r"\b(no,?\s+don'?t|that'?s wrong|that is wrong|not (?:what|like) (?:i|that)|"
    r"revert that|undo that|stop doing that|you keep|again\?|"
    r"same (?:bug|issue|mistake) (?:again|as before)|"
    r"didn'?t (?:i|we) (?:already|just) (?:say|tell|fix))\b",
    re.IGNORECASE,
)

_STATE_FILE_NAME = "session_miner_state.json"
_MAX_EVIDENCE = 240


def state_path() -> Path:
    return registry.home() / _STATE_FILE_NAME


def _load_state() -> dict:
    p = state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _save_state(state: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def transcript_files() -> list[Path]:
    """All Claude Code session transcripts across every project, newest-mtime-first."""
    pattern = str(Path.home() / ".claude" / "projects" / "*" / "*.jsonl")
    paths = [Path(p) for p in glob.glob(pattern)]
    return sorted(paths, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)


def _repo_from_cwd(cwd: str | None) -> str:
    if not cwd:
        return "unknown"
    return Path(cwd).name or "unknown"


def _user_text(entry: dict) -> str | None:
    if entry.get("type") != "user":
        return None
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p) or None
    return None


def scan_lightweight(since_ts: float) -> list[dict]:
    """Cheap regex pass: user-correction phrases in transcripts modified since
    *since_ts*. Returns dicts ready for store.add_rule_suggestion (unsaved)."""
    found: list[dict] = []
    for path in transcript_files():
        try:
            if path.stat().st_mtime < since_ts:
                continue
        except OSError:
            continue
        repo = "unknown"
        try:
            with path.open(encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    cwd = entry.get("cwd")
                    if cwd:
                        repo = _repo_from_cwd(cwd)
                    text = _user_text(entry)
                    if not text or not _CORRECTION_RE.search(text):
                        continue
                    excerpt = " ".join(text.split())[:_MAX_EVIDENCE]
                    found.append({
                        "repo": repo,
                        "text": f"Candidate (unreviewed) — user corrected the model: {excerpt}",
                        "source": f"session:{path.name}#L{lineno}",
                        "evidence": excerpt,
                    })
        except OSError:
            continue
    return found


def run_lightweight(since: float | None = None) -> int:
    """Scan transcripts modified since *since* (or the last high-water mark),
    write pending rule_suggestions, advance the high-water mark. Returns the
    number of suggestions written."""
    state = _load_state()
    start = since if since is not None else float(state.get("last_since", 0.0))
    now = time.time()
    suggestions = scan_lightweight(start)
    for s in suggestions:
        store.add_rule_suggestion(s["repo"], s["text"], s["source"], s["evidence"])
    state["last_since"] = now
    _save_state(state)
    return len(suggestions)


_DEEP_SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "text": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["repo", "text", "quote"],
            },
        },
    },
    "required": ["rules"],
}

_DEEP_PROMPT_HEADER = """\
You are reviewing a raw Claude Code session transcript with FRESH, cold-read
judgment (you were not part of this session and have no stake in defending its
decisions). Extract concrete, durable, one-line rules for mistakes that were
made and corrected or that recurred — the kind of standing lesson that should
be injected into every FUTURE implementer brief for that repo so the same
mistake doesn't happen again. Skip anything vague, one-off, or not actionable.
For each rule give: the repo it applies to (best guess from file paths / cwd
in the transcript), a single imperative sentence, and a short supporting quote
from the transcript. Output only rules you are confident are real, recurring,
or costly mistakes — an empty list is a fine answer if nothing qualifies.

Transcript (JSONL, one event per line):
"""


async def run_deep(model: str = "claude-opus-4-8", limit_files: int = 5) -> int:
    """Fresh-context Claude review pass over transcripts since the last deep run.
    Writes pending rule_suggestions. Returns the number written."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    state = _load_state()
    start = float(state.get("last_deep", 0.0))
    now = time.time()
    files = [p for p in transcript_files() if p.stat().st_mtime >= start][:limit_files]
    written = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.strip():
            continue
        # cheap repo guess for the source tag; the model gives its own per-rule repo
        repo_guess = "unknown"
        for line in text.splitlines()[:200]:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("cwd"):
                repo_guess = _repo_from_cwd(entry["cwd"])
                break
        options = ClaudeAgentOptions(
            model=model, permission_mode="bypassPermissions", allowed_tools=[],
            max_turns=6, disallowed_tools=["Bash", "Write", "Edit", "Read", "Glob", "Grep"],
            output_format={"type": "json_schema", "schema": _DEEP_SCHEMA},
        )
        prompt = _DEEP_PROMPT_HEADER + text[-120_000:]  # bound prompt size; tail = most recent
        result = None
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, ResultMessage):
                result = msg
            elif isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        pass  # structured_output is authoritative; ignore prose
        if result is None or getattr(result, "is_error", False):
            continue
        data = getattr(result, "structured_output", None)
        if not isinstance(data, dict):
            continue
        for r in data.get("rules") or []:
            repo = str(r.get("repo") or repo_guess).strip() or repo_guess
            rtext = str(r.get("text") or "").strip()
            quote = str(r.get("quote") or "")[:_MAX_EVIDENCE]
            if not rtext:
                continue
            store.add_rule_suggestion(repo, rtext, f"session-deep:{path.name}", quote)
            written += 1
    state["last_deep"] = now
    _save_state(state)
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="coxd mine-sessions")
    p.add_argument("--since", type=float, default=None,
                   help="unix ts; only scan transcripts modified since (default: last high-water "
                        "mark)")
    p.add_argument("--deep", action="store_true",
                   help="run the fresh-context LLM review pass instead")
    p.add_argument("--model", default="claude-opus-4-8", help="model for --deep")
    a = p.parse_args(argv)
    if a.deep:
        import asyncio
        n = asyncio.run(run_deep(model=a.model))
    else:
        n = run_lightweight(since=a.since)
    print(f"wrote {n} pending rule suggestion(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

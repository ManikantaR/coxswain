"""Tests for agy queue 5/8: deduped, debounced queue pings, merge order, and copy action.

    cd coxd && python test_agy_notify_queue.py
(invented data only; no network or agy calls.)
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import types
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-agy-notify-")
os.environ.pop("COXD_NTFY_TOPIC", None)
os.environ.pop("COXD_TG_TOKEN", None)
os.environ.pop("COXD_TG_CHAT", None)
os.environ.pop("COXD_QUIET", None)

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:
    _stub = types.ModuleType("claude_agent_sdk")
    for _n in (
        "AssistantMessage",
        "ClaudeSDKClient",
        "RateLimitEvent",
        "TextBlock",
        "ToolUseBlock",
        "query",
    ):
        setattr(_stub, _n, type(_n, (), {}))
    for _n in ("ClaudeAgentOptions", "HookMatcher"):
        setattr(_stub, _n, type(_n, (), {"__init__": lambda self, **kw: None}))
    _stub.ResultMessage = type("ResultMessage", (), {"__init__": lambda self, **kw: None})
    sys.modules["claude_agent_sdk"] = _stub

import agy_handoff  # noqa: E402
import lane  # noqa: E402
import loop  # noqa: E402
import notify  # noqa: E402
import store  # noqa: E402

fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    fails += not cond
    print(("ok  " if cond else "FAIL"), name)
    assert cond, name


@contextmanager
def _patch(obj, attr: str, value):
    old = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, old)


@contextmanager
def _env(**overrides: str | None):
    saved = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _fresh_home():
    tmp = tempfile.mkdtemp(prefix="coxd-agy-notify-case-")
    with _env(
        COXD_HOME=tmp,
        COXD_AGY_PING_DEBOUNCE_S=None,
        COXD_NTFY_TOPIC=None,
        COXD_NTFY_SERVER=None,
        COXD_TG_TOKEN=None,
        COXD_TG_CHAT=None,
        COXD_QUIET=None,
        COXD_BOARD_URL=None,
    ):
        yield Path(tmp)


def _task(
    tid: str,
    repo: str = "widgets",
    state: str = "waiting_agy",
    *,
    issue: int | None = None,
    title: str = "Fix bug",
    lane_name: str | None = "agy-handoff",
    pr_url: str | None = None,
    archived_at: float | None = None,
    created: float = 100.0,
) -> dict:
    brief = title
    if issue is not None:
        url = f"https://github.com/acme/{repo}/issues/{issue}"
        brief = f"{title}\n\nDetails.\n\nGitHub issue: {url}"
    return {
        "id": tid,
        "repo": repo,
        "repo_path": "/tmp/widgets",
        "brief": brief,
        "intent": brief,
        "state": state,
        "lane": lane_name,
        "pr_url": pr_url,
        "archived_at": archived_at,
        "created": created,
    }


def _plan_ev(files: list[str], *, rnd: int = 1, ts: float = 10.0) -> dict:
    return {"kind": "plan", "ts": ts, "data": {"round": rnd, "files": files}}


def _recorder():
    calls: list[dict] = []

    def _rec(title: str, message: str, priority: str = "default", copy_text: str | None = None):
        calls.append({
            "title": title,
            "message": message,
            "priority": priority,
            "copy_text": copy_text,
        })
        return ["ntfy"]

    return calls, _rec


def test_unchanged_state_sends_once() -> None:
    with _fresh_home():
        t = _task("task-test-a", issue=10)
        evs = {"task-test-a": [_plan_ev(["coxd/a.py"])]}
        calls, rec = _recorder()

        with (
            _patch(notify, "notify", rec),
            _patch(agy_handoff, "queue_inputs", lambda: ([t], evs, {}, {})),
        ):
            check("queued inside debounce sends nothing", agy_handoff.notify_queue(now=100.0) == [])
            check("past debounce sends once", agy_handoff.notify_queue(now=701.0) == ["ntfy"])
            check("exactly one message recorded", len(calls) == 1)
            check(
                "second call past debounce sends nothing",
                agy_handoff.notify_queue(now=1400.0) == [],
            )
            check("still only one message", len(calls) == 1)


def test_batched_unblocks() -> None:
    with _fresh_home():
        ta = _task("task-test-a", issue=51, created=10.0)
        tb = _task("task-test-b", issue=52, created=20.0)
        blocker = _task("task-blocker-41", issue=41, state="pr_ready", created=5.0)
        evs_blocked = {
            "task-test-a": [_plan_ev(["shared.py"])],
            "task-test-b": [_plan_ev(["shared.py"])],
            "task-blocker-41": [_plan_ev(["shared.py"])],
        }
        blocker_landed = _task("task-blocker-41", issue=41, state="landed", created=5.0)

        snapshot = [([ta, tb, blocker], evs_blocked, {}, {})]
        calls, rec = _recorder()

        with (
            _patch(notify, "notify", rec),
            _patch(agy_handoff, "queue_inputs", lambda: snapshot[0]),
        ):
            check("blocked snapshot sends nothing", agy_handoff.notify_queue(now=0.0) == [])
            snapshot[0] = ([ta, tb, blocker_landed], evs_blocked, {}, {})
            check("unblock queued inside debounce", agy_handoff.notify_queue(now=100.0) == [])
            check("unblock flushed after debounce", agy_handoff.notify_queue(now=700.0) == ["ntfy"])

        check("exactly one unblock message", len(calls) == 1)
        msg = calls[0]["message"]
        expected = (
            "#41 merged -> ready: "
            "agy-next task-test-a --model gemini-4-argon-high, "
            "agy-next task-test-b --model gemini-4-argon-high"
        )
        check("batched unblock message matches", expected in msg)
        check(
            "copy_text is first ready cmd",
            calls[0]["copy_text"] == "agy-next task-test-a --model gemini-4-argon-high",
        )


def test_debounce_combines_changes() -> None:
    with _fresh_home():
        ta = _task("task-test-a", issue=11, created=10.0)
        tb = _task("task-test-b", issue=12, created=20.0)
        evs_a = {"task-test-a": [_plan_ev(["a.py"])]}
        evs_ab = {
            "task-test-a": [_plan_ev(["a.py"])],
            "task-test-b": [_plan_ev(["b.py"])],
        }
        snapshot = [([ta], evs_a, {}, {})]
        calls, rec = _recorder()

        with (
            _patch(notify, "notify", rec),
            _patch(agy_handoff, "queue_inputs", lambda: snapshot[0]),
        ):
            check(
                "first change inside window sends nothing",
                agy_handoff.notify_queue(now=100.0) == [],
            )
            snapshot[0] = ([ta, tb], evs_ab, {}, {})
            check(
                "second change inside window sends nothing",
                agy_handoff.notify_queue(now=400.0) == [],
            )
            check("still zero messages", len(calls) == 0)
            check(
                "window passes -> one combined send",
                agy_handoff.notify_queue(now=700.0) == ["ntfy"],
            )

        check("one combined message", len(calls) == 1)
        msg = calls[0]["message"]
        check("both tasks in combined message", "task-test-a" in msg and "task-test-b" in msg)


def test_merge_order_and_message() -> None:
    with _fresh_home():
        p1 = _task(
            "task-pr-41",
            issue=41,
            state="pr_ready",
            pr_url="https://github.com/acme/widgets/pull/41",
            created=10.0,
        )
        p2 = _task(
            "task-pr-42",
            issue=42,
            state="pr_ready",
            pr_url="https://github.com/acme/widgets/pull/42",
            created=20.0,
        )
        p3 = _task(
            "task-pr-43",
            issue=43,
            state="pr_ready",
            pr_url="https://github.com/acme/widgets/pull/43",
            created=30.0,
        )
        archived_pr = _task(
            "task-pr-44",
            issue=44,
            state="pr_ready",
            pr_url="https://github.com/acme/widgets/pull/44",
            archived_at=99.0,
            created=5.0,
        )
        claude_pr = _task(
            "task-pr-45",
            issue=45,
            state="pr_ready",
            lane_name=None,
            pr_url="https://github.com/acme/widgets/pull/45",
            created=1.0,
        )
        evs = {
            "task-pr-41": [_plan_ev(["independent.py"])],
            "task-pr-42": [_plan_ev(["shared.py"])],
            "task-pr-43": [_plan_ev(["shared.py", "extra.py"])],
            "task-pr-44": [_plan_ev(["old.py"])],
            "task-pr-45": [_plan_ev(["claude.py"])],
        }

        ordered = agy_handoff.merge_order([
            {**p3, "files": {"shared.py", "extra.py"}},
            {**p1, "files": {"independent.py"}},
            {**p2, "files": {"shared.py"}},
        ])
        check(
            "merge_order oldest-first with overlap after predecessor",
            [p["id"] for p in ordered] == ["task-pr-41", "task-pr-42", "task-pr-43"],
        )

        # Equal/reversed input with overlap: older t2 always before overlapping t3
        check(
            "overlap goes after older PR",
            [p["id"] for p in ordered].index("task-pr-42")
            < [p["id"] for p in ordered].index("task-pr-43"),
        )

        snapshot = [([p3, p2, p1, archived_pr, claude_pr], evs, {}, {})]
        calls, rec = _recorder()

        with (
            _patch(notify, "notify", rec),
            _patch(agy_handoff, "queue_inputs", lambda: snapshot[0]),
        ):
            check(
                "merge order inside debounce sends nothing",
                agy_handoff.notify_queue(now=100.0) == [],
            )
            check(
                "merge order flushes after debounce",
                agy_handoff.notify_queue(now=700.0) == ["ntfy"],
            )
            check(
                "merge order text exact",
                calls[-1]["message"] == "3 PRs ready to merge, in this order: #41, #42, #43",
            )
            check("unchanged merge order sends nothing", agy_handoff.notify_queue(now=1400.0) == [])
            check("still only 1 merge message", len(calls) == 1)

            # Empty list resets prs_ready_sig so future PRs re-announce
            snapshot[0] = ([], {}, {}, {})
            check("empty PR list sends nothing", agy_handoff.notify_queue(now=1500.0) == [])
            snapshot[0] = ([p1], evs, {}, {})
            check("new PR list queues", agy_handoff.notify_queue(now=1600.0) == [])
            check(
                "new PR list sends after debounce",
                agy_handoff.notify_queue(now=2200.0) == ["ntfy"],
            )
            check(
                "re-announced after reset",
                calls[-1]["message"] == "1 PRs ready to merge, in this order: #41",
            )


def test_quiet_hours_uses_min_priority() -> None:
    with _fresh_home(), _env(
        COXD_QUIET="00:00-23:59",
        COXD_NTFY_TOPIC="test-topic",
        COXD_TG_TOKEN="bot-token",
        COXD_TG_CHAT="99999",
        COXD_AGY_PING_DEBOUNCE_S="0",
    ):
        t = _task("task-test-q", issue=61)
        evs = {"task-test-q": [_plan_ev(["q.py"])]}
        ntfy_calls: list[tuple] = []
        tg_calls: list[tuple] = []

        def fake_ntfy(cfg, title, message, url, priority, copy_text=None):
            ntfy_calls.append((title, message, priority, copy_text))

        def fake_tg(cfg, title, message, url, silent=False, copy_text=None):
            tg_calls.append((title, message, silent, copy_text))

        with (
            _patch(notify, "_ntfy", fake_ntfy),
            _patch(notify, "_telegram", fake_tg),
            _patch(agy_handoff, "queue_inputs", lambda: ([t], evs, {}, {})),
        ):
            sent = agy_handoff.notify_queue(now=100.0)

        check("both channels fired in quiet hours", sent == ["ntfy", "telegram"])
        check(
            "ntfy priority is min during quiet hours",
            len(ntfy_calls) == 1 and ntfy_calls[0][2] == "min",
        )
        check(
            "telegram is silent during quiet hours",
            len(tg_calls) == 1 and tg_calls[0][2] is True,
        )


def test_ntfy_copy_action_and_telegram() -> None:
    with _fresh_home(), _env(
        COXD_QUIET="03:00-03:00",  # empty window -> not quiet
        COXD_NTFY_TOPIC="test-topic",
        COXD_NTFY_SERVER="https://ntfy.example",
        COXD_TG_TOKEN="bot-tok",
        COXD_TG_CHAT="12345",
        COXD_AGY_PING_DEBOUNCE_S="0",
    ):
        t = _task("task-test-c", issue=71)
        evs = {"task-test-c": [_plan_ev(["c.py"])]}
        reqs: list[tuple] = []

        def fake_urlopen(req, data=None, timeout=10):
            reqs.append((req, data))
            return None

        with (
            _patch(urllib.request, "urlopen", fake_urlopen),
            _patch(agy_handoff, "queue_inputs", lambda: ([t], evs, {}, {})),
        ):
            sent = agy_handoff.notify_queue(now=100.0)

        expected_cmd = "agy-next task-test-c --model gemini-4-argon-high"
        check("ntfy and telegram sent", sent == ["ntfy", "telegram"] and len(reqs) == 2)
        ntfy_req = reqs[0][0]
        check(
            "ntfy Actions header carries copy command",
            ntfy_req.get_header("Actions") == f"copy, Copy command, {expected_cmd}",
        )
        tg_url, tg_data = reqs[1]
        check("telegram endpoint called", "api.telegram.org/botbot-tok/sendMessage" in tg_url)
        parsed = urllib.parse.parse_qs(tg_data.decode("utf-8"))
        check("telegram text includes command", expected_cmd in parsed["text"][0])

        # Reject unsafe copy_text characters
        rejected = False
        try:
            notify._ntfy(
                {"ntfy_server": "https://ntfy.example", "ntfy_topic": "t"},
                "title",
                "msg",
                None,
                "default",
                copy_text="bad,cmd\ninject",
            )
        except ValueError:
            rejected = True
        check("ntfy rejects comma/semicolon/newline in copy_text", rejected)


def test_queue_ready_event_per_round_and_reannounce() -> None:
    with _fresh_home(), _env(COXD_AGY_PING_DEBOUNCE_S="0"):
        t = _task("task-test-r", issue=81)
        evs_r1 = {"task-test-r": [_plan_ev(["r.py"], rnd=1, ts=10.0)]}
        evs_r2 = {
            "task-test-r": [
                _plan_ev(["r.py"], rnd=1, ts=10.0),
                _plan_ev(["r.py"], rnd=2, ts=20.0),
            ]
        }
        snapshot = [([t], evs_r1, {}, {})]
        calls, rec = _recorder()

        with (
            _patch(notify, "notify", rec),
            _patch(agy_handoff, "queue_inputs", lambda: snapshot[0]),
        ):
            agy_handoff.notify_queue(now=100.0)
            agy_handoff.notify_queue(now=200.0)
            qr1 = [e for e in store.events("task-test-r") if e["kind"] == "queue-ready"]
            check(
                "queue-ready appended once for round 1",
                len(calls) == 1 and len(qr1) == 1 and qr1[0]["data"] == {"round": 1},
            )

            snapshot[0] = ([t], evs_r2, {}, {})
            agy_handoff.notify_queue(now=300.0)
            agy_handoff.notify_queue(now=400.0)
            qr2 = [e for e in store.events("task-test-r") if e["kind"] == "queue-ready"]
            check(
                "new round re-announces and appends round 2 once",
                len(calls) == 2
                and len(qr2) == 2
                and [e["data"] for e in qr2] == [{"round": 1}, {"round": 2}],
            )


def test_regression_old_pings_removed_claude_preserved() -> None:
    with _fresh_home():
        pings: list[tuple] = []

        def fake_async(title: str, message: str, priority: str = "default", copy_text=None):
            pings.append((title, message, priority))

        # 1) agy_handoff.next_round does NOT call notify_async
        store.create_task("task-agy-nr", "widgets", "Brief", "/tmp/wt", lane="agy-handoff")
        with _patch(notify, "notify_async", fake_async):
            agy_handoff.next_round("task-agy-nr", "## Fix\n- a.py\n", "agy", "gate")
        check("next_round does not call notify_async", len(pings) == 0)

        # 2) loop._plan_and_hand_off does NOT call notify_async
        root = Path(tempfile.mkdtemp(prefix="coxd-agy-reg-"))
        origin, repo = root / "origin.git", root / "widgets"
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Avery",
            "GIT_AUTHOR_EMAIL": "avery@example.com",
            "GIT_COMMITTER_NAME": "Avery",
            "GIT_COMMITTER_EMAIL": "avery@example.com",
        }
        for cwd, args in (
            (root, ["init", "--bare", "-b", "main", str(origin)]),
            (root, ["init", "-b", "main", str(repo)]),
        ):
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=git_env)
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        for args in (
            ["add", "."],
            ["commit", "-m", "init"],
            ["remote", "add", "origin", str(origin)],
            ["push", "origin", "main"],
        ):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=git_env)

        store.create_task(
            "task-agy-park", "widgets", "Brief", str(repo), repo_path=str(repo), lane="agy-handoff"
        )

        async def fake_plan(brief, model, emit, effort="medium", *, worktree):
            return lane.PlanOutcome("planned", "## Files\n- a.py\n", 0.01, files=["a.py"])

        with (
            _patch(notify, "notify_async", fake_async),
            _patch(lane, "plan", fake_plan),
        ):
            res = asyncio.run(loop.run_task("task-agy-park"))
        check(
            "loop parks agy at waiting_agy without notify_async",
            res == "waiting_agy" and len(pings) == 0,
        )

        # 3) loop._terminal pr_ready skipped for agy-handoff, preserved for Claude lane
        store.create_task("task-agy-pr", "widgets", "Brief", "/tmp/wt", lane="agy-handoff")
        store.create_task("task-claude-pr", "widgets", "Brief", "/tmp/wt", lane=None)
        with _patch(notify, "notify_async", fake_async):
            loop._terminal("task-agy-pr", "pr_ready", None)
            check("agy-handoff pr_ready skips per-task ping", len(pings) == 0)
            loop._terminal("task-claude-pr", "pr_ready", None)
            check(
                "Claude-lane pr_ready still pings",
                len(pings) == 1
                and pings[0][0] == "coxd · ready to merge"
                and "task-claude-pr" in pings[0][1],
            )


def test_corrupt_state_file_does_not_raise() -> None:
    with _fresh_home() as home:
        state_path = home / "agy_notify_state.json"
        state_path.write_text("{corrupt json!!!", encoding="utf-8")
        t = _task("task-test-corrupt", issue=91)
        evs = {"task-test-corrupt": [_plan_ev(["z.py"])]}
        calls, rec = _recorder()

        with (
            _patch(notify, "notify", rec),
            _patch(agy_handoff, "queue_inputs", lambda: ([t], evs, {}, {})),
        ):
            out1 = agy_handoff.notify_queue(now=100.0)
            check("corrupt JSON treated as empty without raising", out1 == [] and len(calls) == 0)
            # Also test non-dict JSON
            state_path.write_text("[1, 2, 3]", encoding="utf-8")
            out2 = agy_handoff.notify_queue(now=100.0)
            check("non-dict JSON treated as empty without raising", out2 == [])
            out3 = agy_handoff.notify_queue(now=700.0)
            check("recovers and sends after debounce", out3 == ["ntfy"] and len(calls) == 1)


def test_send_exception_does_not_mark_announced() -> None:
    with _fresh_home(), _env(COXD_AGY_PING_DEBOUNCE_S="0"):
        t = _task("task-test-err", issue=92)
        evs = {"task-test-err": [_plan_ev(["e.py"])]}
        calls, rec = _recorder()

        def boom(*_a, **_k):
            raise RuntimeError("network down")

        with _patch(agy_handoff, "queue_inputs", lambda: ([t], evs, {}, {})):
            with _patch(notify, "notify", boom):
                out1 = agy_handoff.notify_queue(now=100.0)
            check("exception caught and nothing sent", out1 == [])
            qr_before = [e for e in store.events("task-test-err") if e["kind"] == "queue-ready"]
            check("queue-ready not appended on failed send", len(qr_before) == 0)
            with _patch(notify, "notify", rec):
                out2 = agy_handoff.notify_queue(now=110.0)
            check("subsequent call retries and succeeds", out2 == ["ntfy"] and len(calls) == 1)


if __name__ == "__main__":
    for test_fn in (
        test_unchanged_state_sends_once,
        test_batched_unblocks,
        test_debounce_combines_changes,
        test_merge_order_and_message,
        test_quiet_hours_uses_min_priority,
        test_ntfy_copy_action_and_telegram,
        test_queue_ready_event_per_round_and_reannounce,
        test_regression_old_pings_removed_claude_preserved,
        test_corrupt_state_file_does_not_raise,
        test_send_exception_does_not_mark_announced,
    ):
        test_fn()
    print("FAILED" if fails else "all passed")
    sys.exit(1 if fails else 0)

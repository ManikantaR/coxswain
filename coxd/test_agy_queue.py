"""Tests for agy queue 3/8: pure queue(), queue_inputs(), caches, and GET /api/agy/queue.

    cd coxd && python test_agy_queue.py
(invented data only; no network or agy calls.)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["COXD_HOME"] = tempfile.mkdtemp(prefix="coxd-agy-queue-")
os.environ.pop("COXD_OWNER_USER", None)
os.environ.pop("COXD_PROXY_SECRET", None)

import agy_handoff  # noqa: E402
import board  # noqa: E402
import ci_triage  # noqa: E402
import store  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

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


def _task(
    tid: str,
    repo: str = "widgets",
    state: str = "waiting_agy",
    *,
    issue: int | None = None,
    title: str = "Fix bug",
    lane: str | None = "agy-handoff",
    pr_url: str | None = None,
    archived_at: float | None = None,
    created: float = 100.0,
    repo_path: str = "/tmp/widgets",
) -> dict:
    brief = title
    if issue is not None:
        url = f"https://github.com/acme/{repo}/issues/{issue}"
        brief = f"{title}\n\nDetails.\n\nGitHub issue: {url}"
    return {
        "id": tid,
        "repo": repo,
        "repo_path": repo_path,
        "brief": brief,
        "intent": brief,
        "state": state,
        "lane": lane,
        "pr_url": pr_url,
        "archived_at": archived_at,
        "created": created,
    }


def _plan_ev(files: list[str], *, rnd: int = 1, ts: float = 10.0) -> dict:
    return {"kind": "plan", "ts": ts, "data": {"round": rnd, "files": files}}


def test_single_ready_candidate() -> None:
    t = _task("t1", issue=12, title="# Add queue endpoint\n\nBody")
    evs = {"t1": [_plan_ev(["coxd/board.py", "coxd/agy_handoff.py"])]}
    rows = agy_handoff.queue([t], evs, {})
    check("single candidate count", len(rows) == 1)
    r = rows[0]
    check("single candidate row shape", r == {
        "id": "t1",
        "repo": "widgets",
        "issue": 12,
        "title": "Add queue endpoint",
        "files": ["coxd/agy_handoff.py", "coxd/board.py"],
        "status": "ready",
        "blocked_by": [],
        "model": "gemini-4-argon-high",
        "cmd": "agy-next t1 --model gemini-4-argon-high",
    })


def test_started_states_blocking() -> None:
    cases = [
        ("queued", None, [], True, "queued"),
        ("gating", None, [], True, "gating"),
        ("reviewing", None, [], True, "reviewing"),
        ("needs_choice", None, [], True, "needs_choice"),
        ("ci-watching", None, [], True, "ci-watching"),
        ("pr_ready", None, [], True, "pr_ready"),
        ("working", None, [], True, "working"),
        ("fixing", None, [], True, "fixing"),
        ("shipping", None, [], True, "shipping"),
        ("needs_human", "https://github.com/acme/widgets/pull/5", [], True, "needs_human+pr_url"),
        ("needs_human", None, [], False, "needs_human_no_pr"),
        ("waiting_agy", None, [{"kind": "agy-status", "ts": 20.0, "data": {}}],
         True, "waiting_agy+status_this_round"),
        ("waiting_agy", None, [{"kind": "agy-status", "ts": 5.0, "data": {}}],
         False, "waiting_agy+stale_status"),
        ("waiting_agy", None, [], False, "waiting_agy_no_status"),
        ("failed", None, [], False, "failed"),
        ("stopped", None, [], False, "stopped"),
    ]
    for state, pr_url, extra_evs, should_block, label in cases:
        cand = _task("cand", issue=10, state="waiting_agy")
        other = _task("other", issue=20, state=state, pr_url=pr_url)
        evs = {
            "cand": [_plan_ev(["a.py"])],
            "other": [_plan_ev(["a.py"], ts=10.0), *extra_evs],
        }
        rows = agy_handoff.queue([cand, other], evs, {})
        cand_row = next(r for r in rows if r["id"] == "cand")
        if should_block:
            check(f"blocks on {label}",
                  cand_row["status"] == "blocked" and cand_row["blocked_by"] == ["#20"])
        else:
            check(f"does not block on {label}",
                  cand_row["status"] == "ready" and cand_row["blocked_by"] == [])


def test_exclusions_and_non_blocking_cases() -> None:
    cand = _task("c1", repo="widgets", issue=1)
    # Non-overlapping file, other-repo same file, landed same file, archived same file, non-agy lane
    other_file = _task("s1", repo="widgets", issue=2, state="gating")
    other_repo = _task("s2", repo="other", issue=3, state="gating")
    landed = _task("s3", repo="widgets", issue=4, state="landed")
    archived = _task("s4", repo="widgets", issue=5, state="gating", archived_at=123.0)
    claude_lane = _task("s5", repo="widgets", issue=6, state="gating", lane=None)
    evs = {
        "c1": [_plan_ev(["a.py"])],
        "s1": [_plan_ev(["b.py"])],
        "s2": [_plan_ev(["a.py"])],
        "s3": [_plan_ev(["a.py"])],
        "s4": [_plan_ev(["a.py"])],
        "s5": [_plan_ev(["a.py"])],
    }
    rows = agy_handoff.queue([cand, other_file, other_repo, landed, archived, claude_lane], evs, {})
    check("only c1 is candidate and ready",
          len(rows) == 1 and rows[0]["id"] == "c1" and rows[0]["status"] == "ready")

    # Archived candidate is skipped
    arch_cand = _task("c2", repo="widgets", issue=7, archived_at=50.0)
    check(
        "archived candidate skipped",
        agy_handoff.queue([arch_cand], {"c2": [_plan_ev(["a.py"])]}, {}) == [],
    )

    # Empty-file candidate is never blocked
    empty_cand = _task("c3", repo="widgets", issue=8)
    blocker = _task("s6", repo="widgets", issue=9, state="gating")
    rows = agy_handoff.queue(
        [empty_cand, blocker],
        {"c3": [_plan_ev([])], "s6": [_plan_ev(["a.py"])]},
        {"widgets": {"PR#1": {"a.py"}}},
    )
    check("empty-file candidate never blocked",
          rows[0]["status"] == "ready" and rows[0]["blocked_by"] == [])

    # Started task without issue number uses its task id
    no_issue_blocker = _task("task-no-issue", repo="widgets", issue=None, state="gating")
    rows = agy_handoff.queue(
        [cand, no_issue_blocker],
        {"c1": [_plan_ev(["a.py"])], "task-no-issue": [_plan_ev(["a.py"])]},
        {},
    )
    check("started task without issue uses task id", rows[0]["blocked_by"] == ["task-no-issue"])


def test_agy_head_excludes_until_next_round_plan() -> None:
    cand = _task("c1", issue=11)
    evs_round1 = {
        "c1": [
            _plan_ev(["a.py"], rnd=1, ts=10.0),
            {"kind": "agy-head", "ts": 15.0, "data": {"sha": "a" * 40}},
        ]
    }
    check("excluded when agy-head in round 1", agy_handoff.queue([cand], evs_round1, {}) == [])

    evs_round2 = {
        "c1": [
            *evs_round1["c1"],
            _plan_ev(["a.py"], rnd=2, ts=20.0),
        ]
    }
    rows = agy_handoff.queue([cand], evs_round2, {})
    check("included again after new plan event", len(rows) == 1 and rows[0]["id"] == "c1")


def test_open_pr_blocking_and_dedupe_sort() -> None:
    cand = _task("c1", repo="widgets", issue=10)
    s1 = _task("s1", repo="widgets", issue=12, state="pr_ready")
    s2 = _task("s2", repo="widgets", issue=3, state="gating")
    evs = {
        "c1": [_plan_ev(["a.py", "b.py"])],
        "s1": [_plan_ev(["a.py"])],
        "s2": [_plan_ev(["b.py"])],
    }
    open_prs = {
        "widgets": {
            "#12": {"a.py"},
            "PR#7": {"b.py"},
            "PR#8": {"unrelated.py"},
        },
        "other": {"PR#99": {"a.py"}},
    }
    rows = agy_handoff.queue([cand, s1, s2], evs, open_prs)
    check("blocked_by deduplicated and sorted",
          rows[0]["status"] == "blocked" and rows[0]["blocked_by"] == ["#12", "#3", "PR#7"])

    # List form of open_pr_files also supported
    rows_list = agy_handoff.queue([cand], {"c1": [_plan_ev(["a.py"])]}, {"widgets": [{"a.py"}]})
    check("open_pr_files list form blocks", rows_list[0]["blocked_by"] == ["PR#1"])

    # Non-overlapping PR does not block
    rows_ok = agy_handoff.queue(
        [cand], {"c1": [_plan_ev(["a.py"])]}, {"widgets": {"PR#8": {"z.py"}}}
    )
    check("non-overlapping PR does not block", rows_ok[0]["status"] == "ready")


def test_ordering() -> None:
    # Mix ready and blocked, with issues, without issues, and different created timestamps.
    tasks = [
        _task("b_low_issue", issue=2, created=1.0),
        _task("r_no_issue_late", issue=None, created=20.0),
        _task("r_issue_20", issue=20, created=5.0),
        _task("r_no_issue_early", issue=None, created=10.0),
        _task("r_issue_5", issue=5, created=30.0),
        _task("b_no_issue", issue=None, created=2.0),
        _task("blocker", issue=99, state="gating", created=0.5),
    ]
    evs = {
        "b_low_issue": [_plan_ev(["shared.py"])],
        "b_no_issue": [_plan_ev(["shared.py"])],
        "blocker": [_plan_ev(["shared.py"])],
        "r_no_issue_late": [_plan_ev(["x1.py"])],
        "r_issue_20": [_plan_ev(["x2.py"])],
        "r_no_issue_early": [_plan_ev(["x3.py"])],
        "r_issue_5": [_plan_ev(["x4.py"])],
    }
    rows = agy_handoff.queue(tasks, evs, {})
    check("ordering ready-first, issue, created", [r["id"] for r in rows] == [
        "r_issue_5",
        "r_issue_20",
        "r_no_issue_early",
        "r_no_issue_late",
        "b_low_issue",
        "b_no_issue",
    ])


def test_model_suggestion_and_title_truncation() -> None:
    long_title = "# " + ("word-" * 30)
    tasks = [
        _task("t_s", issue=1, title=long_title),
        _task("t_m", issue=2),
        _task("t_none", issue=3),
        _task("t_unk", issue=4),
    ]
    evs = {t["id"]: [_plan_ev([f"{t['id']}.py"])] for t in tasks}
    labels = {
        "t_s": {"size:S", "coxd-agy"},
        "t_m": {"size:M"},
        "t_unk": {"bug"},
    }
    rows = {r["id"]: r for r in agy_handoff.queue(tasks, evs, {}, labels)}
    check("size:S -> hydrogen + cmd",
          rows["t_s"]["model"] == "gemini-4-hydrogen-high"
          and rows["t_s"]["cmd"] == "agy-next t_s --model gemini-4-hydrogen-high")
    check("size:M -> argon + cmd",
          rows["t_m"]["model"] == "gemini-4-argon-high"
          and rows["t_m"]["cmd"] == "agy-next t_m --model gemini-4-argon-high")
    check("no label -> argon + cmd",
          rows["t_none"]["model"] == "gemini-4-argon-high"
          and rows["t_none"]["cmd"] == "agy-next t_none --model gemini-4-argon-high")
    check("unknown label -> argon + cmd",
          rows["t_unk"]["model"] == "gemini-4-argon-high"
          and rows["t_unk"]["cmd"] == "agy-next t_unk --model gemini-4-argon-high")
    check("title stripped and truncated <=120",
          not rows["t_s"]["title"].startswith("#") and len(rows["t_s"]["title"]) == 120)


def test_queue_is_pure() -> None:
    def boom(*_a, **_k):
        raise AssertionError("pure queue() must not call store or subprocess")

    t = _task("p1", issue=1)
    evs = {"p1": [_plan_ev(["a.py"])]}
    with (
        _patch(store, "list_tasks", boom),
        _patch(store, "get_task", boom),
        _patch(store, "events", boom),
        _patch(subprocess, "run", boom),
    ):
        rows = agy_handoff.queue([t], evs, {})
    check("queue is pure (no store/subprocess)", len(rows) == 1 and rows[0]["id"] == "p1")


def test_pr_files_cache_and_gh_failures() -> None:
    agy_handoff._PR_FILES_CACHE.clear()
    agy_handoff._ISSUE_LABEL_CACHE.clear()
    diff_calls: list[list[str]] = []

    def fake_gh(args: list[str], timeout: float = 10.0) -> tuple[int, str]:
        if args[:2] == ["pr", "diff"]:
            diff_calls.append(args)
            if "7" in args:
                return 0, "a.py\nb.py\n"
            return 1, ""
        if args[:2] == ["issue", "view"]:
            raise RuntimeError("boom")
        return 1, ""

    with _patch(agy_handoff, "_gh", fake_gh):
        f1 = agy_handoff._pr_files("acme/widgets", 7, "sha1")
        f2 = agy_handoff._pr_files("acme/widgets", 7, "sha1")
        check(
            "PR-files cache hits on same sha",
            f1 == {"a.py", "b.py"} and f2 == f1 and len(diff_calls) == 1,
        )
        f3 = agy_handoff._pr_files("acme/widgets", 7, "sha2")
        check(
            "PR-files cache misses on changed sha",
            f3 == {"a.py", "b.py"} and len(diff_calls) == 2,
        )
        check("gh failure on issue view falls back to empty labels",
              agy_handoff._issue_labels("acme/widgets", 5) == set())


def test_endpoint_get_and_read_only() -> None:
    agy_handoff._PR_FILES_CACHE.clear()
    agy_handoff._ISSUE_LABEL_CACHE.clear()

    b1 = "Small ready task\n\nGitHub issue: https://github.com/acme/widgets/issues/101"
    b2 = "Blocked task\n\nGitHub issue: https://github.com/acme/widgets/issues/102"
    b3 = "Active blocker\n\nGitHub issue: https://github.com/acme/widgets/issues/103"
    store.create_task(
        "q-ready", "widgets", b1, "/tmp/w1", repo_path="/tmp/widgets", lane="agy-handoff"
    )
    store.set_state("q-ready", "waiting_agy")
    store.append_event("q-ready", "plan", {"round": 1, "files": ["coxd/free.py"]})

    store.create_task(
        "q-blocked", "widgets", b2, "/tmp/w2", repo_path="/tmp/widgets", lane="agy-handoff"
    )
    store.set_state("q-blocked", "waiting_agy")
    store.append_event("q-blocked", "plan", {"round": 1, "files": ["coxd/shared.py"]})

    store.create_task(
        "q-active", "widgets", b3, "/tmp/w3", repo_path="/tmp/widgets", lane="agy-handoff"
    )
    store.set_state("q-active", "gating")
    store.append_event("q-active", "plan", {"round": 1, "files": ["coxd/shared.py"]})

    store.create_task(
        "q-claude", "widgets", "Non-agy", "/tmp/w4", repo_path="/tmp/widgets", lane=None
    )
    store.set_state("q-claude", "waiting_agy")

    tids = ("q-ready", "q-blocked", "q-active", "q-claude")
    before_counts = {tid: len(store.events(tid)) for tid in tids}

    def fake_gh(args: list[str], timeout: float = 10.0) -> tuple[int, str]:
        if args[:2] == ["pr", "list"]:
            return 0, json.dumps([])
        if args[:2] == ["issue", "view"]:
            num = int(args[2])
            lbs = [{"name": "size:S"}] if num == 101 else [{"name": "size:M"}]
            return 0, json.dumps({"labels": lbs})
        raise AssertionError(f"unexpected gh call: {args}")

    client = TestClient(board.app, base_url="http://localhost:8791")
    with (
        _patch(ci_triage, "repo_slug", lambda _p: "acme/widgets"),
        _patch(agy_handoff, "_gh", fake_gh),
    ):
        resp = client.get("/api/agy/queue")
        post_resp = client.post("/api/agy/queue")

    check("GET /api/agy/queue -> 200", resp.status_code == 200)
    check("POST /api/agy/queue -> 405", post_resp.status_code == 405)
    body = resp.json()
    check(
        "payload top-level keys and counts",
        set(body) == {"queue", "ready", "blocked"}
        and body["ready"] == 1
        and body["blocked"] == 1,
    )
    q = body["queue"]
    check("queue row ids and order", [r["id"] for r in q] == ["q-ready", "q-blocked"])
    expected_keys = {
        "id", "repo", "issue", "title", "files", "status", "blocked_by", "model", "cmd"
    }
    check("row keys exact", all(set(r) == expected_keys for r in q))
    check("size:S label resolved via gh", q[0]["model"] == "gemini-4-hydrogen-high")
    check("blocked_by populated from gating task", q[1]["blocked_by"] == ["#103"])

    after_counts = {tid: len(store.events(tid)) for tid in tids}
    check("endpoint appends no events", after_counts == before_counts)


def test_queue_inputs_open_prs_and_label_ttl() -> None:
    agy_handoff._PR_FILES_CACHE.clear()
    agy_handoff._ISSUE_LABEL_CACHE.clear()
    gh_calls: list[list[str]] = []

    def fake_gh(args: list[str], timeout: float = 10.0) -> tuple[int, str]:
        gh_calls.append(args)
        if args[:2] == ["pr", "list"]:
            return 0, json.dumps([
                {"number": 7, "headRefName": "coxd/unknown-task", "headRefOid": "sha7", "url": ""},
                {"number": 8, "headRefName": "coxd-agy/q-active", "headRefOid": "sha8", "url": ""},
                {"number": 9, "headRefName": "feature/human-pr", "headRefOid": "sha9", "url": ""},
            ])
        if args[:2] == ["pr", "diff"]:
            num = args[2]
            if num == "7":
                return 0, "coxd/free.py\n"
            if num == "8":
                return 0, "coxd/shared.py\n"
            raise AssertionError(f"non-coxd PR should not fetch diff: {args}")
        if args[:2] == ["issue", "view"]:
            return 0, json.dumps({"labels": [{"name": "size:S"}]})
        raise AssertionError(f"unexpected gh call: {args}")

    with (
        _patch(ci_triage, "repo_slug", lambda _p: "acme/widgets"),
        _patch(agy_handoff, "_gh", fake_gh),
    ):
        tasks, evs, open_prs, labels = agy_handoff.queue_inputs()
        rows = agy_handoff.queue(tasks, evs, open_prs, labels)
        q_ready = next(r for r in rows if r["id"] == "q-ready")
        check("open PR PR#7 blocks q-ready; coxd-agy/q-active resolves to #103",
              open_prs.get("widgets") == {"PR#7": {"coxd/free.py"}, "#103": {"coxd/shared.py"}}
              and q_ready["status"] == "blocked"
              and q_ready["blocked_by"] == ["PR#7"])

        # Second label lookup within TTL makes no extra gh issue view call
        before_view = sum(1 for a in gh_calls if a[:2] == ["issue", "view"])
        lbs = agy_handoff._issue_labels("acme/widgets", 101)
        after_view = sum(1 for a in gh_calls if a[:2] == ["issue", "view"])
        check("label cache hits within TTL", lbs == {"size:S"} and after_view == before_view)


if __name__ == "__main__":
    for test_fn in (
        test_single_ready_candidate,
        test_started_states_blocking,
        test_exclusions_and_non_blocking_cases,
        test_agy_head_excludes_until_next_round_plan,
        test_open_pr_blocking_and_dedupe_sort,
        test_ordering,
        test_model_suggestion_and_title_truncation,
        test_queue_is_pure,
        test_pr_files_cache_and_gh_failures,
        test_endpoint_get_and_read_only,
        test_queue_inputs_open_prs_and_label_ttl,
    ):
        test_fn()
    print("FAILED" if fails else "all passed")
    sys.exit(1 if fails else 0)

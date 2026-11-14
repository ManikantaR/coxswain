import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "coxd"))
import roles  # noqa: E402


def test_defaults(monkeypatch):
    for k in ("WORKER_MODEL", "WORKER_EFFORT", "REVIEW_MODEL", "REVIEW_EFFORT"):
        monkeypatch.delenv("COXD_" + k, raising=False)
    c = roles.resolve(None)
    assert c == {"worker_model": "claude-sonnet-5-5", "worker_effort": "medium",
                 "review_model": "claude-opus-5-5", "review_effort": "medium",
                 "planner_model": "claude-sonnet-5-5", "planner_effort": "medium"}


def test_precedence(monkeypatch):
    monkeypatch.setenv("COXD_WORKER_EFFORT", "low")
    monkeypatch.setenv("COXD_REVIEW_MODEL", "m-env")
    c = roles.resolve({"worker_effort": "max"})
    assert c["worker_effort"] == "max" and c["review_model"] == "m-env"
    assert roles.resolve({})["worker_effort"] == "low"


def test_invalid_effort_falls_through(monkeypatch):
    monkeypatch.setenv("COXD_WORKER_EFFORT", "bogus")
    assert roles.resolve({"worker_effort": "nope"})["worker_effort"] == "medium"


def test_effort_reaches_sdk(monkeypatch):
    import asyncio
    import sys
    import types

    # CI is zero-network without claude_agent_sdk: stub it, and force fresh
    # imports of lane/loop (restored by monkeypatch) so they bind to the stub.
    stub = types.ModuleType("claude_agent_sdk")
    for n in ("AssistantMessage", "ClaudeAgentOptions", "ClaudeSDKClient",
              "HookMatcher", "RateLimitEvent", "ResultMessage", "TextBlock",
              "ToolUseBlock", "query"):
        setattr(stub, n, type(n, (), {}))
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", stub)
    monkeypatch.delitem(sys.modules, "lane", raising=False)
    monkeypatch.delitem(sys.modules, "loop", raising=False)
    import lane
    import loop
    import registry
    seen = {}

    async def fake_worker(wt, prompt, model, emit, resume=None, effort="medium", **k):
        seen["w"] = (model, effort)
        raise RuntimeError("stop")

    monkeypatch.setattr(lane, "run_worker", fake_worker)
    monkeypatch.setattr(loop.store, "get_task", lambda t: {
        "worktree": "/tmp/x", "repo": "r", "brief": "b"})
    monkeypatch.setattr(loop.store, "set_state", lambda *a, **k: None)
    monkeypatch.setattr(loop.store, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(loop.worktree, "provision", lambda wt: {})
    monkeypatch.setattr(registry, "load", lambda r: {"worker_effort": "xhigh"})
    monkeypatch.delenv("COXD_WORKER_MODEL", raising=False)
    try:
        asyncio.run(loop.run_task("t"))
    except RuntimeError:
        pass
    assert seen["w"] == ("claude-sonnet-5-5", "xhigh")


def test_invalid_effort_warns_and_defaults_medium(monkeypatch, capsys):
    monkeypatch.setenv("COXD_REVIEW_EFFORT", "turbo")
    assert roles.resolve(None)["review_effort"] == "medium"
    assert "invalid review_effort" in capsys.readouterr().err

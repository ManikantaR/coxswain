"""Repo registry + defanged clone-on-demand (DESIGN-VNEXT D17)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cox import dispatch, proc, repos, server
from cox.dispatch import DispatchError


@pytest.fixture(autouse=True)
def clone_root(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("COX_REPO_ROOT", str(root))
    return root


def _mk_repo(root: Path, name: str) -> Path:
    p = root / name
    (p / ".git").mkdir(parents=True)
    return p


def test_is_git_url_and_slug():
    assert repos.is_git_url("https://github.com/o/r.git")
    assert repos.is_git_url("git@github.com:o/r.git")
    assert not repos.is_git_url("~/repo/coxswain")
    assert not repos.is_git_url("coxswain")
    assert repos.slug_from_url("https://github.com/o/read-aloud-tts.git") == "read-aloud-tts"
    assert repos.slug_from_url("git@ssh.dev.azure.com:v3/org/proj/my-repo") == "my-repo"
    assert repos.slug_from_url("https://host/o/r/") == "r"


def test_preexisting_repos_are_trusted_and_listed(clone_root):
    _mk_repo(clone_root, "coxswain")
    _mk_repo(clone_root, "relay")
    (clone_root / "not-a-repo").mkdir()  # no .git -> excluded

    names = {r.name: r for r in repos.list_local_repos()}
    assert set(names) == {"coxswain", "relay"}
    assert names["coxswain"].trusted is True  # captain put it there -> trusted


def test_resolve_local_name_and_path(clone_root):
    _mk_repo(clone_root, "coxswain")
    r = repos.resolve("coxswain")
    assert r.path == (clone_root / "coxswain").resolve()
    assert r.cloned is False and r.trusted is True and r.url is None


def test_clone_marks_untrusted_then_trust(clone_root, monkeypatch):
    made = {}

    def fake_run(cmd, **kw):
        assert cmd[:2] == ["git", "clone"]
        assert "--no-recurse-submodules" in cmd  # defanged
        assert "core.hooksPath=/dev/null" in cmd
        dest = Path(cmd[-1])
        (dest / ".git").mkdir(parents=True)
        made["dest"] = dest
        return proc.ProcResult(0, "", "")

    monkeypatch.setattr(repos.proc, "run", fake_run)
    res = repos.clone("https://github.com/o/read-aloud-tts.git")
    assert res.cloned is True and res.trusted is False
    assert res.path == (clone_root / "read-aloud-tts").resolve()
    assert repos.is_trusted(res.path) is False

    repos.mark_trusted(res.path)
    assert repos.is_trusted(res.path) is True

    # second resolve of the same URL dedups (no re-clone) and is now trusted
    res2 = repos.clone("https://github.com/o/read-aloud-tts.git")
    assert res2.cloned is False and res2.trusted is True


def test_dispatch_refuses_untrusted_repo(clone_root, monkeypatch):
    dest = _mk_repo(clone_root, "fresh")
    repos._mark_untrusted(dest)
    with pytest.raises(DispatchError, match="not yet trusted"):
        dispatch.dispatch(
            repo_path=dest, title="x", body="x",
            path=dispatch.DispatchPath.FULL, lane="stub",
        )


def test_server_add_and_trust_roundtrip(clone_root, monkeypatch):
    def fake_run(cmd, **kw):
        (Path(cmd[-1]) / ".git").mkdir(parents=True)
        return proc.ProcResult(0, "", "")

    monkeypatch.setattr(repos.proc, "run", fake_run)
    out = server.add_repo("https://github.com/o/aura-tutor.git")
    assert out["cloned"] is True and out["needs_trust"] is True
    path = out["path"]

    # it shows up in the picker, flagged untrusted
    listing = {r["name"]: r for r in server.list_repos()["repos"]}
    assert listing["aura-tutor"]["trusted"] is False

    assert server.trust_repo(path)["trusted"] is True
    listing = {r["name"]: r for r in server.list_repos()["repos"]}
    assert listing["aura-tutor"]["trusted"] is True


def test_server_add_repo_surfaces_error(clone_root, monkeypatch):
    def boom(cmd, **kw):
        raise proc.BosunProcError(cmd, 128, "", "repository not found")

    monkeypatch.setattr(repos.proc, "run", boom)
    assert "error" in server.add_repo("https://github.com/o/nope.git")
    assert "error" in server.add_repo("")  # empty ref

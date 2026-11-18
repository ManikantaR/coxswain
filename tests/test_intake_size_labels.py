"""Tests for agy queue size labels in coxd/intake.py and lead ordering docs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "coxd"))

import intake  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _fake_gh(calls: list[list[str]], existing: list[str]):
    def _gh(args: list[str]) -> tuple[int, str]:
        calls.append(args)
        if args[:2] == ["label", "list"]:
            return 0, json.dumps([{"name": name} for name in existing])
        if args[:2] == ["label", "create"]:
            return 0, ""
        raise AssertionError(f"unexpected gh args: {args}")

    return _gh


def test_labels_dict_contains_existing_and_size_labels():
    expected = {
        "coxd-agy",
        "coxd-ready",
        "coxd-working",
        "coxd-waiting",
        "coxd-untrusted",
        "coxd-pii-block",
        "coxd-needs-human",
        "size:S",
        "size:M",
    }
    assert expected.issubset(intake.LABELS)
    assert "size:L" not in intake.LABELS


def test_ensure_labels_creates_missing_size_labels_and_caches(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(intake, "_gh", _fake_gh(calls, ["coxd-ready"]))
    monkeypatch.setattr(intake, "_labels_ready", set())

    intake._ensure_labels("acme/widgets")

    creates = {c[2]: c for c in calls if c[:2] == ["label", "create"]}
    assert "size:S" in creates
    assert "size:M" in creates
    for name in ("size:S", "size:M"):
        desc, color = intake.LABELS[name]
        assert creates[name] == [
            "label",
            "create",
            name,
            "-R",
            "acme/widgets",
            "--description",
            desc,
            "--color",
            color,
        ]

    count_after_first = len(calls)
    intake._ensure_labels("acme/widgets")
    assert len(calls) == count_after_first


def test_ensure_labels_skips_create_when_all_exist(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(intake, "_gh", _fake_gh(calls, list(intake.LABELS)))
    monkeypatch.setattr(intake, "_labels_ready", set())

    intake._ensure_labels("acme/widgets")

    assert [c[:2] for c in calls] == [["label", "list"]]


def test_skill_md_documents_agy_queue_ordering_rules():
    text = (ROOT / "plugins" / "coxd" / "skills" / "coxd" / "SKILL.md").read_text()
    for phrase in ("size:S", "size:M", "Hydrogen", "Argon", "Depends on #N"):
        assert phrase in text

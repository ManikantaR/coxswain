"""Scaffold smoke test — replaced by real suites as TASKS.md lands (T-01+)."""

from cox.cli import main


def test_unimplemented_subcommand_exits_2(capsys):
    assert main(["status"]) == 2
    assert "TASKS.md" in capsys.readouterr().err

"""Integration tests for `mship debug` sub-app. See #30."""
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


def test_debug_hypothesis_writes_journal_entry(configured_git_app: Path):
    runner.invoke(app, ["spawn", "hypo test", "--repos", "shared", "--skip-setup"])
    result = runner.invoke(
        app, ["debug", "hypothesis", "test is flaky",
              "--evidence", "test-runs/5", "--task", "hypo-test"],
    )
    assert result.exit_code == 0, result.output
    log = (configured_git_app / ".mothership" / "logs" / "hypo-test.md").read_text()
    assert "action=hypothesis" in log
    assert "test is flaky" in log
    assert "id=" in log  # auto-generated
    assert "evidence=" in log and "test-runs/5" in log


def test_debug_hypothesis_honors_explicit_id(configured_git_app: Path):
    runner.invoke(app, ["spawn", "id test", "--repos", "shared", "--skip-setup"])
    result = runner.invoke(
        app, ["debug", "hypothesis", "H1", "--id", "h1", "--task", "id-test"],
    )
    assert result.exit_code == 0, result.output
    log = (configured_git_app / ".mothership" / "logs" / "id-test.md").read_text()
    assert "id=h1" in log


def test_debug_rule_out_writes_parent_kv(configured_git_app: Path):
    runner.invoke(app, ["spawn", "ro test", "--repos", "shared", "--skip-setup"])
    runner.invoke(
        app, ["debug", "hypothesis", "H1", "--id", "h1", "--task", "ro-test"],
    )
    result = runner.invoke(
        app, ["debug", "rule-out", "not it", "--parent", "h1", "--task", "ro-test"],
    )
    assert result.exit_code == 0, result.output
    log = (configured_git_app / ".mothership" / "logs" / "ro-test.md").read_text()
    assert "action=ruled-out" in log
    assert "parent=h1" in log


def test_debug_rule_out_with_category(configured_git_app: Path):
    runner.invoke(app, ["spawn", "cat test", "--repos", "shared", "--skip-setup"])
    runner.invoke(app, ["debug", "hypothesis", "H", "--id", "h", "--task", "cat-test"])
    result = runner.invoke(
        app, ["debug", "rule-out", "R", "--parent", "h",
              "--category", "tool-output-misread", "--task", "cat-test"],
    )
    assert result.exit_code == 0, result.output
    log = (configured_git_app / ".mothership" / "logs" / "cat-test.md").read_text()
    assert "category=tool-output-misread" in log


def test_debug_resolved_writes_entry(configured_git_app: Path):
    runner.invoke(app, ["spawn", "res test", "--repos", "shared", "--skip-setup"])
    runner.invoke(app, ["debug", "hypothesis", "H", "--id", "h", "--task", "res-test"])
    result = runner.invoke(
        app, ["debug", "resolved", "fixed by commit abc", "--task", "res-test"],
    )
    assert result.exit_code == 0, result.output
    log = (configured_git_app / ".mothership" / "logs" / "res-test.md").read_text()
    assert "action=debug-resolved" in log
    assert "fixed by commit abc" in log


def test_debug_resolved_without_hypothesis_warns(configured_git_app: Path):
    """Advisory stderr warning when closing without any prior hypothesis."""
    runner.invoke(app, ["spawn", "warn test", "--repos", "shared", "--skip-setup"])
    result = runner.invoke(
        app, ["debug", "resolved", "no thread", "--task", "warn-test"],
    )
    # Entry still written, exit 0.
    assert result.exit_code == 0, result.output
    # Warning surfaced.
    assert "warning" in (result.output or "").lower()
    assert "no prior hypothesis" in (result.output or "").lower() or "without any prior hypothesis" in (result.output or "").lower()
    log = (configured_git_app / ".mothership" / "logs" / "warn-test.md").read_text()
    assert "action=debug-resolved" in log


def test_debug_auto_id_is_8_char_hex(configured_git_app: Path):
    """Auto-generated id is 8 lowercase-hex chars."""
    import re as _re
    runner.invoke(app, ["spawn", "auto id", "--repos", "shared", "--skip-setup"])
    runner.invoke(app, ["debug", "hypothesis", "H", "--task", "auto-id"])
    log = (configured_git_app / ".mothership" / "logs" / "auto-id.md").read_text()
    m = _re.search(r"id=([a-f0-9]+)", log)
    assert m is not None
    assert len(m.group(1)) == 8

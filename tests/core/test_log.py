from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.log import LogManager, LogEntry


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".mothership" / "logs"
    d.mkdir(parents=True)
    return d


def test_create_log(logs_dir: Path):
    mgr = LogManager(logs_dir)
    mgr.create("add-labels")
    assert (logs_dir / "add-labels.md").exists()
    content = (logs_dir / "add-labels.md").read_text()
    assert "# Task Log: add-labels" in content


def test_append_entry(logs_dir: Path):
    mgr = LogManager(logs_dir)
    mgr.create("add-labels")
    mgr.append("add-labels", "Refactored auth controller")
    content = (logs_dir / "add-labels.md").read_text()
    assert "Refactored auth controller" in content


def test_append_multiple(logs_dir: Path):
    mgr = LogManager(logs_dir)
    mgr.create("add-labels")
    mgr.append("add-labels", "First entry")
    mgr.append("add-labels", "Second entry")
    entries = mgr.read("add-labels")
    messages = [e.message for e in entries]
    assert "First entry" in messages
    assert "Second entry" in messages


def test_read_empty_log(logs_dir: Path):
    mgr = LogManager(logs_dir)
    mgr.create("add-labels")
    entries = mgr.read("add-labels")
    assert entries == []


def test_read_last_n(logs_dir: Path):
    mgr = LogManager(logs_dir)
    mgr.create("add-labels")
    mgr.append("add-labels", "First")
    mgr.append("add-labels", "Second")
    mgr.append("add-labels", "Third")
    entries = mgr.read("add-labels", last=2)
    assert len(entries) == 2
    assert entries[0].message == "Second"
    assert entries[1].message == "Third"


def test_read_nonexistent_log(logs_dir: Path):
    mgr = LogManager(logs_dir)
    entries = mgr.read("nonexistent")
    assert entries == []


def test_log_entry_has_timestamp(logs_dir: Path):
    mgr = LogManager(logs_dir)
    mgr.create("add-labels")
    mgr.append("add-labels", "Test entry")
    entries = mgr.read("add-labels")
    assert len(entries) == 1
    assert isinstance(entries[0].timestamp, datetime)
    assert entries[0].message == "Test entry"


def test_log_message_containing_hash_headers(logs_dir: Path):
    """Messages with ## in the body should not break the parser."""
    mgr = LogManager(logs_dir)
    mgr.create("hash-test")
    mgr.append("hash-test", "Fixed issue with ## preventing commits")
    mgr.append("hash-test", "Second entry after hash")
    entries = mgr.read("hash-test")
    assert len(entries) == 2
    assert "## preventing commits" in entries[0].message
    assert entries[1].message == "Second entry after hash"


def test_log_entry_defaults_to_none_for_new_fields():
    e = LogEntry(timestamp=datetime.now(timezone.utc), message="m")
    assert e.repo is None
    assert e.iteration is None
    assert e.test_state is None
    assert e.action is None
    assert e.open_question is None


def test_parse_old_format_entry(tmp_path):
    path = tmp_path / "old.md"
    path.write_text(
        "# Task Log: old\n\n"
        "## 2026-04-14T12:00:00Z\nlegacy message\n"
    )
    log_mgr = LogManager(tmp_path)
    # Create the matching task file name
    (tmp_path / "old.md").write_text(
        "# Task Log: old\n\n"
        "## 2026-04-14T12:00:00Z\nlegacy message\n"
    )
    entries = log_mgr.read("old")
    assert len(entries) == 1
    assert entries[0].message == "legacy message"
    assert entries[0].repo is None
    assert entries[0].iteration is None


def test_parse_new_format_entry(tmp_path):
    (tmp_path / "t.md").write_text(
        "# Task Log: t\n\n"
        "## 2026-04-14T12:00:00Z  repo=shared  iter=3  test=pass  action=implementing\n"
        "Implemented Label type\n"
    )
    log_mgr = LogManager(tmp_path)
    entries = log_mgr.read("t")
    assert len(entries) == 1
    e = entries[0]
    assert e.message == "Implemented Label type"
    assert e.repo == "shared"
    assert e.iteration == 3
    assert e.test_state == "pass"
    assert e.action == "implementing"


def test_parse_quoted_value_with_spaces(tmp_path):
    (tmp_path / "t.md").write_text(
        "# Task Log: t\n\n"
        '## 2026-04-14T12:00:00Z  open="how to handle null workspace"  repo=auth\n'
        "Stuck\n"
    )
    log_mgr = LogManager(tmp_path)
    (entry,) = log_mgr.read("t")
    assert entry.open_question == "how to handle null workspace"
    assert entry.repo == "auth"


def test_parse_mixed_old_and_new_entries(tmp_path):
    (tmp_path / "t.md").write_text(
        "# Task Log: t\n\n"
        "## 2026-04-14T12:00:00Z\nlegacy\n\n"
        "## 2026-04-14T12:05:00Z  repo=shared\nstructured\n"
    )
    log_mgr = LogManager(tmp_path)
    entries = log_mgr.read("t")
    assert len(entries) == 2
    assert entries[0].message == "legacy"
    assert entries[0].repo is None
    assert entries[1].message == "structured"
    assert entries[1].repo == "shared"


def test_append_writes_new_kv_fields(tmp_path: Path):
    """id/parent/evidence/category are stored as kv on the journal line."""
    from mship.core.log import LogManager
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    mgr.append(
        "t", "test hypothesis",
        action="hypothesis",
        id="a3f4c2e1",
        evidence="test-runs/5",
    )
    content = (tmp_path / "logs" / "t.md").read_text()
    assert "id=a3f4c2e1" in content
    assert "evidence=" in content and "test-runs/5" in content


def test_read_parses_new_kv_fields(tmp_path: Path):
    from mship.core.log import LogManager
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    mgr.append(
        "t", "refuted because TZ is fixed",
        action="ruled-out",
        id="b7d9e2a0",
        parent="a3f4c2e1",
        evidence="test-runs/6",
        category="tool-output-misread",
    )
    entries = mgr.read("t")
    assert len(entries) == 1
    e = entries[0]
    assert e.id == "b7d9e2a0"
    assert e.parent == "a3f4c2e1"
    assert e.evidence == "test-runs/6"
    assert e.category == "tool-output-misread"


def test_append_backcompat_no_new_kv(tmp_path: Path):
    """Existing callers (no new kwargs) still produce identical output."""
    from mship.core.log import LogManager
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    mgr.append("t", "plain message", action="committed")
    content = (tmp_path / "logs" / "t.md").read_text()
    assert "id=" not in content
    assert "parent=" not in content
    assert "evidence=" not in content
    assert "category=" not in content

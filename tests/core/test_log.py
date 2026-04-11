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

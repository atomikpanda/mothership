"""Tests for current_debug_thread. See #30."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.log import LogManager


def _at(mgr: LogManager, slug: str, **kwargs) -> None:
    """Append helper."""
    mgr.append(slug, kwargs.pop("msg", "x"), **kwargs)


def test_empty_journal_returns_none(tmp_path: Path):
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    assert current_debug_thread(mgr, "t") is None


def test_journal_with_no_hypotheses_returns_none(tmp_path: Path):
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    _at(mgr, "t", msg="did a commit", action="committed")
    _at(mgr, "t", msg="ran tests", action="ran tests", iteration=1, test_state="pass")
    assert current_debug_thread(mgr, "t") is None


def test_single_open_hypothesis_returns_one_entry(tmp_path: Path):
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    _at(mgr, "t", msg="flaky assertion", action="hypothesis", id="h1")
    thread = current_debug_thread(mgr, "t")
    assert thread is not None
    assert len(thread) == 1
    assert thread[0].action == "hypothesis"
    assert thread[0].id == "h1"


def test_hypothesis_plus_ruled_out_still_open(tmp_path: Path):
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    _at(mgr, "t", msg="H1", action="hypothesis", id="h1")
    _at(mgr, "t", msg="R1", action="ruled-out", id="r1", parent="h1")
    thread = current_debug_thread(mgr, "t")
    assert thread is not None
    assert len(thread) == 2


def test_closed_thread_returns_none(tmp_path: Path):
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    _at(mgr, "t", msg="H1", action="hypothesis", id="h1")
    _at(mgr, "t", msg="R1", action="ruled-out", id="r1", parent="h1")
    _at(mgr, "t", msg="done", action="debug-resolved", id="res1")
    assert current_debug_thread(mgr, "t") is None


def test_reopened_thread_returns_only_new_segment(tmp_path: Path):
    """Close + reopen: return only the new-segment entries."""
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    _at(mgr, "t", msg="H1", action="hypothesis", id="h1")
    _at(mgr, "t", msg="done1", action="debug-resolved", id="res1")
    _at(mgr, "t", msg="H2", action="hypothesis", id="h2")
    _at(mgr, "t", msg="R2", action="ruled-out", id="r2", parent="h2")
    thread = current_debug_thread(mgr, "t")
    assert thread is not None
    ids = [e.id for e in thread]
    assert ids == ["h2", "r2"]


def test_resolved_without_prior_hypothesis_returns_none(tmp_path: Path):
    """A resolved entry with no hypothesis before it doesn't constitute a thread."""
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    _at(mgr, "t", msg="weird", action="debug-resolved", id="res1")
    assert current_debug_thread(mgr, "t") is None


def test_interleaved_non_debug_entries_included(tmp_path: Path):
    """Test runs, commits, etc. during an open thread are part of the thread."""
    from mship.core.debug import current_debug_thread
    mgr = LogManager(tmp_path / "logs")
    mgr.create("t")
    _at(mgr, "t", msg="H1", action="hypothesis", id="h1")
    _at(mgr, "t", msg="iter 3: 2/3", action="ran tests", iteration=3, test_state="mixed", parent="h1")
    _at(mgr, "t", msg="code change", action="committed")
    thread = current_debug_thread(mgr, "t")
    assert thread is not None
    assert len(thread) == 3

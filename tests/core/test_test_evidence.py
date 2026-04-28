"""Tests for the unified test-evidence reader. See #81."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.log import LogManager
from mship.core.state import Task, TestResult
from mship.util.shell import ShellResult, ShellRunner


def _make_task(**overrides) -> Task:
    defaults = dict(
        slug="t",
        description="d",
        phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["a", "b"],
        branch="feat/t",
    )
    defaults.update(overrides)
    return Task(**defaults)


def test_evidence_prefers_test_results_over_journal(tmp_path: Path):
    from mship.core.test_evidence import read_evidence
    log = LogManager(tmp_path / "logs")
    log.create("t")
    # Journal says fail, but task.test_results says pass — test_results wins.
    log.append("t", "ran pytest", repo="a", test_state="fail")
    task = _make_task(
        affected_repos=["a"],
        test_results={"a": TestResult(status="pass", at=datetime.now(timezone.utc))},
    )
    ev = read_evidence(task, log)
    assert ev["a"].status == "passed"
    assert ev["a"].source == "test_results"


def test_evidence_journal_newer_than_test_results_wins(tmp_path: Path):
    """Regression of #81 (issue #108): when `mship journal --test-state pass`
    is logged AFTER an `mship test` failure, the newer journal entry wins.

    Previously the reader unconditionally preferred test_results, which made
    `mship journal --test-state pass` ineffective as a workaround when
    `mship test` couldn't run (e.g., Taskfile lacks a `test` target).
    """
    from mship.core.test_evidence import read_evidence
    log = LogManager(tmp_path / "logs")
    log.create("t")
    # mship test ran 5 minutes ago, failed.
    earlier = datetime.now(timezone.utc) - timedelta(minutes=5)
    task = _make_task(
        affected_repos=["a"],
        test_results={"a": TestResult(status="fail", at=earlier)},
    )
    # User then journaled a pass entry now (newer than test_results).
    log.append("t", "tests pass after fixing", repo="a", test_state="pass")
    ev = read_evidence(task, log)
    assert ev["a"].status == "passed"
    assert ev["a"].source == "journal"


def test_evidence_uses_latest_per_repo_journal_when_no_test_results(tmp_path: Path):
    from mship.core.test_evidence import read_evidence
    log = LogManager(tmp_path / "logs")
    log.create("t")
    log.append("t", "early fail", repo="a", test_state="fail")
    log.append("t", "later pass", repo="a", test_state="pass")
    task = _make_task(affected_repos=["a"])
    ev = read_evidence(task, log)
    assert ev["a"].status == "passed"
    assert ev["a"].source == "journal"


def test_evidence_global_journal_applies_to_all_repos(tmp_path: Path):
    """An entry with no repo= tag provides evidence for every affected repo."""
    from mship.core.test_evidence import read_evidence
    log = LogManager(tmp_path / "logs")
    log.create("t")
    log.append("t", "ran pytest in worktree", test_state="pass")
    task = _make_task(affected_repos=["a", "b"])
    ev = read_evidence(task, log)
    assert ev["a"].status == "passed"
    assert ev["b"].status == "passed"


def test_evidence_per_repo_entry_wins_over_global(tmp_path: Path):
    from mship.core.test_evidence import read_evidence
    log = LogManager(tmp_path / "logs")
    log.create("t")
    log.append("t", "global pass", test_state="pass")
    log.append("t", "per-repo fail", repo="a", test_state="fail")
    task = _make_task(affected_repos=["a", "b"])
    ev = read_evidence(task, log)
    assert ev["a"].status == "failed"  # per-repo wins
    assert ev["b"].status == "passed"  # global applies


def test_evidence_missing_when_nothing(tmp_path: Path):
    from mship.core.test_evidence import read_evidence
    log = LogManager(tmp_path / "logs")
    log.create("t")
    task = _make_task(affected_repos=["a"])
    ev = read_evidence(task, log)
    assert ev["a"].status == "missing"
    assert ev["a"].source == "none"


def test_evidence_skip_status_is_non_blocking(tmp_path: Path):
    """A repo with TestResult status='skip' (declared not_applicable: [test])
    yields a 'skipped' evidence status — not 'missing' (which would warn).
    See #109.
    """
    from mship.core.test_evidence import read_evidence
    log = LogManager(tmp_path / "logs")
    log.create("t")
    task = _make_task(
        affected_repos=["fixtures"],
        test_results={"fixtures": TestResult(status="skip", at=datetime.now(timezone.utc))},
    )
    ev = read_evidence(task, log)
    assert ev["fixtures"].status == "skipped"
    assert ev["fixtures"].source == "test_results"


def test_format_missing_summary_does_not_warn_on_skipped(tmp_path: Path):
    """Skipped repos must not appear in the missing/stale/failed warning lines."""
    from mship.core.test_evidence import read_evidence, format_missing_summary
    log = LogManager(tmp_path / "logs")
    log.create("t")
    task = _make_task(
        affected_repos=["fixtures", "real"],
        test_results={
            "fixtures": TestResult(status="skip", at=datetime.now(timezone.utc)),
            "real": TestResult(status="pass", at=datetime.now(timezone.utc)),
        },
    )
    ev = read_evidence(task, log)
    assert format_missing_summary(ev) == []  # nothing to warn about


def test_evidence_stale_when_branch_has_newer_commit(tmp_path: Path):
    """Pass evidence older than the repo's task-branch HEAD → 'stale'."""
    from mship.core.test_evidence import read_evidence
    log = LogManager(tmp_path / "logs")
    log.create("t")
    evidence_at = datetime.now(timezone.utc) - timedelta(hours=1)
    task = _make_task(
        affected_repos=["a"],
        test_results={"a": TestResult(status="pass", at=evidence_at)},
    )
    # Commit timestamp is NEWER than the evidence.
    head_ts = (datetime.now(timezone.utc)).isoformat()
    shell = MagicMock(spec=ShellRunner)
    shell.run.return_value = ShellResult(returncode=0, stdout=head_ts + "\n", stderr="")
    ev = read_evidence(task, log, shell=shell, repo_paths={"a": Path("/tmp/a")})
    assert ev["a"].status == "stale"


def test_evidence_not_stale_when_commit_older_than_evidence(tmp_path: Path):
    from mship.core.test_evidence import read_evidence
    log = LogManager(tmp_path / "logs")
    log.create("t")
    evidence_at = datetime.now(timezone.utc)
    task = _make_task(
        affected_repos=["a"],
        test_results={"a": TestResult(status="pass", at=evidence_at)},
    )
    head_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    shell = MagicMock(spec=ShellRunner)
    shell.run.return_value = ShellResult(returncode=0, stdout=head_ts + "\n", stderr="")
    ev = read_evidence(task, log, shell=shell, repo_paths={"a": Path("/tmp/a")})
    assert ev["a"].status == "passed"


def test_evidence_stale_check_gracefully_skipped_when_no_shell(tmp_path: Path):
    """Without shell, we can't check stale — just report passed."""
    from mship.core.test_evidence import read_evidence
    log = LogManager(tmp_path / "logs")
    log.create("t")
    task = _make_task(
        affected_repos=["a"],
        test_results={"a": TestResult(
            status="pass", at=datetime.now(timezone.utc) - timedelta(hours=1),
        )},
    )
    ev = read_evidence(task, log)
    assert ev["a"].status == "passed"


def test_format_missing_summary_empty_when_all_pass(tmp_path: Path):
    from mship.core.test_evidence import read_evidence, format_missing_summary
    log = LogManager(tmp_path / "logs")
    log.create("t")
    log.append("t", "pass", test_state="pass")
    task = _make_task(affected_repos=["a", "b"])
    ev = read_evidence(task, log)
    assert format_missing_summary(ev) == []


def test_format_missing_summary_groups_by_status(tmp_path: Path):
    from mship.core.test_evidence import read_evidence, format_missing_summary
    log = LogManager(tmp_path / "logs")
    log.create("t")
    log.append("t", "b failed", repo="b", test_state="fail")
    task = _make_task(affected_repos=["a", "b", "c"])
    ev = read_evidence(task, log)
    lines = format_missing_summary(ev)
    text = "\n".join(lines)
    assert "a" in text and "c" in text  # both missing
    assert "b" in text  # failing
    assert any("missing" in l.lower() or "not run" in l.lower() for l in lines)
    assert any("failing" in l.lower() for l in lines)

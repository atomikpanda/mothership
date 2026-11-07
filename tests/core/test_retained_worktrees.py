from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.retained_worktrees import (
    RetainedWorktreeStore,
    RetainedWorktreeStoreError,
)
from mship.core.state import DependencyEdge, Task, TestResult


NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def _task(tmp_path: Path) -> Task:
    return Task(
        slug="retained-task",
        description="Retained worktree snapshot",
        phase="review",
        created_at=NOW,
        affected_repos=["api", "web"],
        worktrees={
            "api": tmp_path / "worktrees" / "api",
            "web": tmp_path / "worktrees" / "web",
        },
        branch="feat/retained-task",
        test_results={"unit": TestResult(status="pass", at=NOW)},
        blocked_reason="waiting for review",
        blocked_at=NOW,
        pr_urls={"api": "https://example.test/api/1"},
        finished_at=NOW,
        phase_entered_at=NOW,
        last_activity_at=NOW,
        active_repo="api",
        last_switched_at_sha={"api": {"main": "a" * 40}},
        test_iteration=2,
        base_branch="main",
        base_override="feat/base",
        passive_repos={"web"},
        spec_id="spec-1",
        depends_on=[DependencyEdge(upstream_slug="upstream", created_at=NOW)],
        work_item_id="item-1",
    )


def test_retain_round_trips_full_task_snapshot_across_store_reloads(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    task = _task(tmp_path)
    store = RetainedWorktreeStore(state_dir)

    first = store.retain(task)
    first_bytes = first.read_bytes()
    second = store.retain(task)

    assert first != second
    assert first.read_bytes() == first_bytes
    assert first.parent == state_dir / "retained-worktrees"
    assert stat.S_IMODE(first.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    payload = json.loads(first.read_text())
    assert set(payload) == {"version", "retained_at", "task"}
    assert payload["version"] == 1
    assert datetime.fromisoformat(payload["retained_at"]).tzinfo is not None
    assert payload["task"]["worktrees"] == {
        "api": str(task.worktrees["api"]),
        "web": str(task.worktrees["web"]),
    }

    records = RetainedWorktreeStore(state_dir).list_records()
    assert {path: restored for path, restored in records} == {
        first: task,
        second: task,
    }


def test_list_records_fails_closed_for_malformed_record(tmp_path: Path) -> None:
    directory = tmp_path / ".mothership" / "retained-worktrees"
    directory.mkdir(parents=True)
    malformed = directory / "broken.json"
    malformed.write_text("not json")

    with pytest.raises(RetainedWorktreeStoreError, match="could not read"):
        RetainedWorktreeStore(tmp_path / ".mothership").list_records()

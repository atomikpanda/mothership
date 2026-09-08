from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from mship.core.persistence.database import WorkspaceDatabase
from mship.core.persistence.migration import (
    MigrationPreflightError,
    MigrationVerificationError,
    _backup_legacy,
    _legacy_fingerprints,
    migrate_legacy_state,
)
from mship.core.persistence.schema import storage_metadata, tasks
from mship.core.state import (
    DependencyEdge,
    StateManager,
    Task,
    TestResult,
    WorkspaceState,
)
from mship.core.workitem import ExternalLink, WorkItem
from mship.core.workitem_store import WorkItemStore

NOW = datetime(2026, 9, 7, 20, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class LegacyWorkspace:
    state_dir: Path
    expected_state: WorkspaceState
    expected_items: list[WorkItem]
    message_sentinel: Path
    spec_sentinel: Path


@pytest.fixture
def legacy_workspace(tmp_path: Path) -> LegacyWorkspace:
    state_dir = tmp_path / ".mothership"
    workitems_dir = state_dir / "workitems"
    workitems_dir.mkdir(parents=True)

    upstream = Task(
        slug="upstream",
        description="library",
        phase="review",
        created_at=NOW,
        affected_repos=["shared"],
        worktrees={"shared": tmp_path / "upstream-shared"},
        branch="feat/upstream",
        test_results={"shared": TestResult(status="pass", at=NOW)},
        pr_urls={"shared": "https://github.example/shared/pull/1"},
        active_repo="shared",
        last_switched_at_sha={"shared": {"api": "abc123"}},
        test_iteration=3,
        base_branch="main",
        passive_repos={"docs"},
        spec_id="spec-upstream",
        work_item_id="wi-active",
    )
    downstream = Task(
        slug="downstream",
        description="service",
        phase="dev",
        created_at=NOW,
        affected_repos=["api"],
        branch="feat/downstream",
        depends_on=[DependencyEdge(upstream_slug="upstream", created_at=NOW)],
        work_item_id="wi-archived",
    )
    expected_state = WorkspaceState(
        tasks={"downstream": downstream, "upstream": upstream}
    )
    (state_dir / "state.yaml").write_text(
        yaml.safe_dump(expected_state.model_dump(mode="json"))
    )

    active = WorkItem.model_validate(
        {
            "id": "wi-active",
            "title": "Active",
            "workspace": "test",
            "kind": "feature",
            "created_at": NOW,
            "updated_at": NOW,
            "spec_id": "spec-upstream",
            "plan_path": "docs/plans/upstream.md",
            "task_slugs": ["upstream"],
            "thread_ids": ["thread-active"],
            "external_links": [
                ExternalLink(
                    provider="github",
                    url="https://github.example/issues/1",
                    title="Issue 1",
                )
            ],
            "affected_repos": ["shared"],
            "pr_urls": ["https://github.example/shared/pull/1"],
            "future_field": {"preserve": True},
        }
    )
    archived = WorkItem(
        id="wi-archived",
        title="Archived",
        workspace="test",
        kind="chore",
        created_at=NOW,
        updated_at=NOW,
        task_slugs=["downstream"],
        thread_ids=["thread-archived"],
        archived=True,
    )
    for item in (active, archived):
        (workitems_dir / f"{item.id}.json").write_text(item.model_dump_json(indent=2))

    message_sentinel = state_dir / "messages" / "thread.json"
    message_sentinel.parent.mkdir()
    message_sentinel.write_text('{"unchanged": true}\n')
    spec_sentinel = tmp_path / "specs" / "spec-upstream.md"
    spec_sentinel.parent.mkdir()
    spec_sentinel.write_text("# unchanged\n")
    return LegacyWorkspace(
        state_dir=state_dir,
        expected_state=expected_state,
        expected_items=[active, archived],
        message_sentinel=message_sentinel,
        spec_sentinel=spec_sentinel,
    )


def test_migration_activates_only_after_verified_import(
    legacy_workspace: LegacyWorkspace,
) -> None:
    report = migrate_legacy_state(
        legacy_workspace.state_dir,
        daemon_probe=lambda: None,
        now=NOW,
    )

    assert report.migrated is True
    assert report.tasks == 2
    assert report.work_items == 2
    assert report.database_path.is_file()
    assert report.backup_path is not None
    assert (report.backup_path / "state.yaml").is_file()
    assert (report.backup_path / "workitems" / "wi-active.json").is_file()
    assert not (legacy_workspace.state_dir / "state.yaml").exists()
    assert not (legacy_workspace.state_dir / "workitems").exists()
    assert StateManager(legacy_workspace.state_dir).load() == (
        legacy_workspace.expected_state
    )
    actual_items = WorkItemStore(legacy_workspace.state_dir / "workitems").list(
        include_archived=True
    )
    assert {item.id: item for item in actual_items} == {
        item.id: item for item in legacy_workspace.expected_items
    }
    assert legacy_workspace.message_sentinel.read_text() == ('{"unchanged": true}\n')
    assert legacy_workspace.spec_sentinel.read_text() == "# unchanged\n"

    database = WorkspaceDatabase(legacy_workspace.state_dir)
    with database.read() as connection:
        metadata = dict(
            connection.execute(select(storage_metadata.c.key, storage_metadata.c.value))
            .tuples()
            .all()
        )
    assert metadata["migrated_at"] == NOW.isoformat()
    assert len(metadata["legacy_state_sha256"]) == 64
    assert len(metadata["legacy_workitems_sha256"]) == 64


def test_migration_preserves_switch_source_without_dependencies(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    task = Task(
        slug="no-dependencies",
        description="switch source without usable dependencies",
        phase="dev",
        created_at=NOW,
        affected_repos=["mothership"],
        branch="feat/no-dependencies",
        last_switched_at_sha={"mothership": {}},
    )
    expected = WorkspaceState(tasks={task.slug: task})
    (state_dir / "state.yaml").write_text(
        yaml.safe_dump(expected.model_dump(mode="json"))
    )

    report = migrate_legacy_state(
        state_dir,
        daemon_probe=lambda: None,
        now=NOW,
    )

    assert report.migrated is True
    assert StateManager(state_dir).load() == expected
    assert not (state_dir / "state.yaml").exists()


def test_migration_accepts_semantically_equivalent_naive_timestamps(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    workitems_dir = state_dir / "workitems"
    workitems_dir.mkdir(parents=True)
    naive = datetime(2026, 9, 7, 20, 0)
    task = Task(
        slug="naive-task",
        description="legacy naive timestamps",
        phase="dev",
        created_at=naive,
        affected_repos=["mothership"],
        branch="feat/naive-task",
        test_results={"mothership": TestResult(status="pass", at=naive)},
        phase_entered_at=naive,
        work_item_id="wi-naive",
    )
    item = WorkItem(
        id="wi-naive",
        title="Naive timestamps",
        workspace="test",
        kind="chore",
        created_at=naive,
        updated_at=naive,
        task_slugs=[task.slug],
    )
    (state_dir / "state.yaml").write_text(
        yaml.safe_dump(WorkspaceState(tasks={task.slug: task}).model_dump(mode="json"))
    )
    (workitems_dir / f"{item.id}.json").write_text(item.model_dump_json(indent=2))

    report = migrate_legacy_state(
        state_dir,
        daemon_probe=lambda: None,
        now=NOW,
    )

    assert report.migrated is True
    restored = StateManager(state_dir).load().tasks[task.slug]
    assert restored.created_at == naive.replace(tzinfo=timezone.utc)
    restored_item = WorkItemStore(state_dir / "workitems").get(item.id)
    assert restored_item is not None
    assert restored_item.updated_at == naive.replace(tzinfo=timezone.utc)


def test_migration_rejects_non_timestamp_candidate_mismatch(
    legacy_workspace: LegacyWorkspace,
) -> None:
    def corrupt_candidate(stage: str) -> None:
        if stage != "verify":
            return
        candidate_path = next(
            path
            for path in legacy_workspace.state_dir.glob(
                ".mothership.db.migrating-*"
            )
            if not path.name.endswith(("-wal", "-shm"))
        )
        candidate = WorkspaceDatabase(
            legacy_workspace.state_dir,
            database_path=candidate_path,
        )
        with candidate.write() as connection:
            connection.execute(
                tasks.update()
                .where(tasks.c.slug == "upstream")
                .values(description="corrupted")
            )

    with pytest.raises(MigrationVerificationError, match="does not match"):
        migrate_legacy_state(
            legacy_workspace.state_dir,
            daemon_probe=lambda: None,
            now=NOW,
            stage_hook=corrupt_candidate,
        )

    assert (legacy_workspace.state_dir / "state.yaml").is_file()
    assert (legacy_workspace.state_dir / "workitems").is_dir()
    assert not WorkspaceDatabase(legacy_workspace.state_dir).path.exists()


@pytest.mark.parametrize(
    "stage",
    ["validate", "backup", "alembic", "import", "verify", "activate"],
)
def test_migration_fault_leaves_legacy_live_and_retryable(
    legacy_workspace: LegacyWorkspace,
    stage: str,
) -> None:
    def fail_at(current: str) -> None:
        if current == stage:
            raise RuntimeError(f"injected {stage} failure")

    with pytest.raises(RuntimeError, match=f"injected {stage} failure"):
        migrate_legacy_state(
            legacy_workspace.state_dir,
            daemon_probe=lambda: None,
            now=NOW,
            stage_hook=fail_at,
        )

    assert not WorkspaceDatabase(legacy_workspace.state_dir).path.exists()
    assert (legacy_workspace.state_dir / "state.yaml").is_file()
    assert (legacy_workspace.state_dir / "workitems").is_dir()
    assert not list(legacy_workspace.state_dir.glob("*.migrating-*"))
    assert legacy_workspace.message_sentinel.read_text() == ('{"unchanged": true}\n')
    assert legacy_workspace.spec_sentinel.read_text() == "# unchanged\n"

    report = migrate_legacy_state(
        legacy_workspace.state_dir,
        daemon_probe=lambda: None,
        now=NOW,
    )
    assert report.migrated is True
    assert report.database_path.is_file()


def test_migration_refuses_while_daemon_is_running(
    legacy_workspace: LegacyWorkspace,
) -> None:
    with pytest.raises(MigrationPreflightError, match="daemon"):
        migrate_legacy_state(
            legacy_workspace.state_dir,
            daemon_probe=lambda: {"pid": 42},
            now=NOW,
        )

    assert not WorkspaceDatabase(legacy_workspace.state_dir).path.exists()
    assert (legacy_workspace.state_dir / "state.yaml").is_file()


def test_migration_rejects_invalid_legacy_data(
    legacy_workspace: LegacyWorkspace,
) -> None:
    (legacy_workspace.state_dir / "workitems" / "wi-active.json").write_text("{")

    with pytest.raises(Exception):
        migrate_legacy_state(
            legacy_workspace.state_dir,
            daemon_probe=lambda: None,
            now=NOW,
        )

    assert not WorkspaceDatabase(legacy_workspace.state_dir).path.exists()
    assert (legacy_workspace.state_dir / "state.yaml").is_file()


def test_legacy_fingerprints_reject_external_workitem_symlink(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    workitems_dir = state_dir / "workitems"
    workitems_dir.mkdir(parents=True)
    outside = WorkItem(
        id="outside",
        title="Outside",
        workspace="test",
        kind="chore",
        created_at=NOW,
        updated_at=NOW,
    )
    outside_path = state_dir / "outside.json"
    outside_path.write_text(outside.model_dump_json())
    (workitems_dir / "linked.json").symlink_to(outside_path)

    with pytest.raises(ValueError, match="unsafe work item id"):
        _legacy_fingerprints(state_dir)


def test_legacy_backup_rejects_external_workitem_symlink(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    workitems_dir = state_dir / "workitems"
    workitems_dir.mkdir(parents=True)
    outside = WorkItem(
        id="outside",
        title="Outside",
        workspace="test",
        kind="chore",
        created_at=NOW,
        updated_at=NOW,
    )
    outside_path = state_dir / "outside.json"
    outside_path.write_text(outside.model_dump_json())
    (workitems_dir / "linked.json").symlink_to(outside_path)

    with pytest.raises(ValueError, match="unsafe work item id"):
        _backup_legacy(state_dir, NOW)


def test_migration_rejects_external_workitem_symlink_before_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".mothership"
    workitems_dir = state_dir / "workitems"
    workitems_dir.mkdir(parents=True)
    (state_dir / "state.yaml").write_text("tasks: {}\n")
    outside = WorkItem(
        id="outside",
        title="Outside",
        workspace="test",
        kind="chore",
        created_at=NOW,
        updated_at=NOW,
    )
    outside_path = state_dir / "outside.json"
    outside_path.write_text(outside.model_dump_json())
    (workitems_dir / "linked.json").symlink_to(outside_path)
    monkeypatch.setattr(
        "mship.core.persistence.migration.list_legacy_workitems",
        lambda *_args, **_kwargs: ([], False),
    )

    with pytest.raises(ValueError, match="unsafe work item id"):
        migrate_legacy_state(
            state_dir,
            daemon_probe=lambda: None,
            now=NOW,
        )

    assert (state_dir / "state.yaml").is_file()
    assert not WorkspaceDatabase(state_dir).path.exists()


def test_retirement_failure_restores_legacy_authority_and_is_retryable(
    legacy_workspace: LegacyWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_rename = Path.rename
    failed = False

    def fail_workitems_retirement(path: Path, target: Path):
        nonlocal failed
        if path.name == "workitems" and not failed:
            failed = True
            raise OSError("injected retirement failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_workitems_retirement)

    with pytest.raises(OSError, match="retirement failure"):
        migrate_legacy_state(
            legacy_workspace.state_dir,
            daemon_probe=lambda: None,
            now=NOW,
        )

    assert not WorkspaceDatabase(legacy_workspace.state_dir).path.exists()
    assert (legacy_workspace.state_dir / "state.yaml").is_file()
    assert (legacy_workspace.state_dir / "workitems").is_dir()
    assert not list(legacy_workspace.state_dir.glob("state.yaml.migrated-*"))
    assert not list(legacy_workspace.state_dir.glob("workitems.migrated-*"))

    report = migrate_legacy_state(
        legacy_workspace.state_dir,
        daemon_probe=lambda: None,
        now=NOW,
    )
    assert report.migrated is True
    assert StateManager(legacy_workspace.state_dir).load() == (
        legacy_workspace.expected_state
    )

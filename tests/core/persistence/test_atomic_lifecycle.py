from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.persistence.lifecycle_repository import LifecycleRepository
from mship.core.persistence.workspace_store import WorkspaceStore
from mship.core.state import StateManager, Task
from mship.core.workitem import WorkItem
from mship.core.workitem_lifecycle import TaskMetadataRetentionConflictError
from mship.core.workitem_store import TaskLinkAmbiguousError, WorkItemStore
from tests.persistence_helpers import corrupt_workitem


NOW = datetime(2026, 9, 7, 20, 0, tzinfo=timezone.utc)


class InjectedFailure(RuntimeError):
    pass


def _task(slug: str = "task-a", *, work_item_id: str | None = None) -> Task:
    return Task(
        slug=slug,
        description="Atomic lifecycle",
        phase="dev",
        created_at=NOW,
        affected_repos=["api", "web"],
        worktrees={
            "api": Path("/tmp/api"),
            "web": Path("/tmp/web"),
        },
        branch=f"feat/{slug}",
        pr_urls={
            "api": "https://example.test/pr/1",
            "web": "https://example.test/pr/2",
            "mirror": "https://example.test/pr/1",
        },
        work_item_id=work_item_id,
    )


@pytest.fixture
def lifecycle_stores(tmp_path: Path):
    state_dir = tmp_path / ".mothership"
    workspace = WorkspaceStore(state_dir)
    state = StateManager(workspace_store=workspace)
    items = WorkItemStore(
        state_dir / "workitems",
        workspace_store=workspace,
    )
    lifecycle = LifecycleRepository(workspace)
    return lifecycle, state, items


def _item(items: WorkItemStore, title: str = "Owner") -> WorkItem:
    return items.create(title, "feature", "test", NOW)


def test_register_task_and_owner_roll_back_together(
    lifecycle_stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, state, items = lifecycle_stores
    item = _item(items)
    monkeypatch.setattr(
        lifecycle,
        "_checkpoint",
        lambda stage: (
            (_ for _ in ()).throw(InjectedFailure("after task insert"))
            if stage == "after_task_insert"
            else None
        ),
    )

    with pytest.raises(InjectedFailure, match="after task insert"):
        lifecycle.register_task(_task(), item.id, now=NOW)

    assert state.load().tasks.get("task-a") is None
    assert items.get(item.id).task_slugs == []


def test_register_missing_workitem_rolls_back_task(lifecycle_stores) -> None:
    lifecycle, state, _items = lifecycle_stores

    with pytest.raises(KeyError, match="missing-item"):
        lifecycle.register_task(_task(), "missing-item", now=NOW)

    assert state.load().tasks == {}


def test_retain_and_delete_is_atomic(
    lifecycle_stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, state, items = lifecycle_stores
    item = _item(items)
    task = _task(work_item_id=item.id)
    lifecycle.register_task(task, item.id, now=NOW)
    before_item = items.get(item.id)
    monkeypatch.setattr(
        lifecycle,
        "_checkpoint",
        lambda stage: (
            (_ for _ in ()).throw(InjectedFailure("before task delete"))
            if stage == "before_task_delete"
            else None
        ),
    )

    with pytest.raises(InjectedFailure, match="before task delete"):
        lifecycle.retain_and_delete_task(task.slug, now=NOW)

    assert state.load().tasks[task.slug] == task
    assert items.get(item.id) == before_item


def test_historical_owner_is_authoritative_and_duplicate_is_refused(
    lifecycle_stores,
) -> None:
    lifecycle, state, items = lifecycle_stores
    first = _item(items, "First")
    second = _item(items, "Second")
    lifecycle.register_task(_task(work_item_id=first.id), first.id, now=NOW)
    assert lifecycle.retain_and_delete_task("task-a", now=NOW) is True

    with pytest.raises(TaskLinkAmbiguousError) as error:
        lifecycle.register_task(
            _task(work_item_id=second.id),
            second.id,
            now=NOW,
        )

    assert error.value.item_ids == sorted([first.id, second.id])
    assert state.load().tasks == {}
    assert items.get(first.id).task_slugs == ["task-a"]
    assert items.get(second.id).task_slugs == []


def test_retention_deduplicates_repositories_and_pr_urls(
    lifecycle_stores,
) -> None:
    lifecycle, state, items = lifecycle_stores
    item = _item(items)
    item.affected_repos = ["api"]
    item.pr_urls = ["https://example.test/pr/1"]
    items.save(item)
    lifecycle.register_task(_task(work_item_id=item.id), item.id, now=NOW)

    assert lifecycle.retain_and_delete_task("task-a", now=NOW) is True

    assert state.load().tasks == {}
    retained = items.get(item.id)
    assert retained.affected_repos == ["api", "web"]
    assert retained.pr_urls == [
        "https://example.test/pr/1",
        "https://example.test/pr/2",
    ]


def test_bind_spec_updates_both_sides_in_one_transaction(
    lifecycle_stores,
) -> None:
    lifecycle, state, items = lifecycle_stores
    item = _item(items)
    state.insert_task(_task())

    lifecycle.bind_spec("task-a", item.id, "spec-1", now=NOW)

    task = state.load().tasks["task-a"]
    assert task.work_item_id == item.id
    assert task.spec_id == "spec-1"
    linked = items.get(item.id)
    assert linked.task_slugs == ["task-a"]
    assert linked.spec_id == "spec-1"


def test_bind_spec_rolls_back_task_when_workitem_update_fails(
    lifecycle_stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, state, items = lifecycle_stores
    item = _item(items)
    state.insert_task(_task())
    monkeypatch.setattr(
        lifecycle,
        "_checkpoint",
        lambda stage: (
            (_ for _ in ()).throw(InjectedFailure("after task link"))
            if stage == "after_task_link"
            else None
        ),
    )

    with pytest.raises(InjectedFailure, match="after task link"):
        lifecycle.bind_spec("task-a", item.id, "spec-1", now=NOW)

    task = state.load().tasks["task-a"]
    assert task.work_item_id is None
    assert task.spec_id is None
    assert items.get(item.id).task_slugs == []


def test_partial_prune_keeps_task_then_final_prune_retains_and_deletes(
    lifecycle_stores,
) -> None:
    lifecycle, state, items = lifecycle_stores
    item = _item(items)
    lifecycle.register_task(_task(work_item_id=item.id), item.id, now=NOW)

    assert (
        lifecycle.retain_and_prune_task_repos(
            "task-a",
            ["api"],
            now=NOW,
        )
        is False
    )
    current = state.load().tasks["task-a"]
    assert current.worktrees == {"web": Path("/tmp/web")}
    assert items.get(item.id).affected_repos == []

    assert (
        lifecycle.retain_and_prune_task_repos(
            "task-a",
            ["web"],
            now=NOW,
            expected_task=current,
        )
        is True
    )
    assert state.load().tasks == {}
    assert items.get(item.id).affected_repos == ["api", "web"]


def test_external_snapshot_change_refuses_delete(lifecycle_stores) -> None:
    lifecycle, state, items = lifecycle_stores
    item = _item(items)
    task = _task(work_item_id=item.id)
    lifecycle.register_task(task, item.id, now=NOW)
    expected = state.load().tasks[task.slug]
    state.mutate_task(
        task.slug,
        lambda live: live.pr_urls.update({"new": "https://example.test/pr/new"}),
    )

    with pytest.raises(TaskMetadataRetentionConflictError, match="changed"):
        lifecycle.retain_and_delete_task(
            task.slug,
            now=NOW,
            expected_task=expected,
        )

    assert task.slug in state.load().tasks
    assert "https://example.test/pr/new" not in items.get(item.id).pr_urls


def test_batch_delete_rolls_back_every_task_on_snapshot_conflict(
    lifecycle_stores,
) -> None:
    lifecycle, state, items = lifecycle_stores
    first_item = _item(items, "First")
    second_item = _item(items, "Second")
    first = _task("first", work_item_id=first_item.id)
    second = _task("second", work_item_id=second_item.id)
    lifecycle.register_task(first, first_item.id, now=NOW)
    lifecycle.register_task(second, second_item.id, now=NOW)
    expected = state.load().tasks
    state.mutate_task(
        "second",
        lambda task: task.pr_urls.update({"new": "https://example.test/pr/new"}),
    )

    with pytest.raises(TaskMetadataRetentionConflictError, match="changed"):
        lifecycle.retain_and_delete_tasks(expected, now=NOW)

    assert set(state.load().tasks) == {"first", "second"}
    assert items.get(first_item.id).affected_repos == []
    assert items.get(second_item.id).affected_repos == []


def test_record_pr_urls_updates_both_sides_and_deduplicates(
    lifecycle_stores,
) -> None:
    lifecycle, state, items = lifecycle_stores
    item = _item(items)
    task = _task(work_item_id=item.id)
    task.pr_urls = {}
    lifecycle.register_task(task, item.id, now=NOW)

    url = "https://example.test/pr/3"
    lifecycle.record_pr_urls("task-a", {"api": url}, now=NOW)
    lifecycle.record_pr_urls(
        "task-a",
        {"api": url, "web": url},
        now=NOW,
    )

    assert state.load().tasks["task-a"].pr_urls == {
        "api": url,
        "web": url,
    }
    retained = items.get(item.id)
    assert retained.affected_repos == ["api", "web"]
    assert retained.pr_urls == [url]


def test_record_pr_urls_rolls_back_task_when_workitem_update_fails(
    lifecycle_stores,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle, state, items = lifecycle_stores
    item = _item(items)
    task = _task(work_item_id=item.id)
    task.pr_urls = {}
    lifecycle.register_task(task, item.id, now=NOW)
    before_item = items.get(item.id)
    monkeypatch.setattr(
        lifecycle,
        "_checkpoint",
        lambda stage: (
            (_ for _ in ()).throw(InjectedFailure("after task replace"))
            if stage == "after_task_replace"
            else None
        ),
    )

    with pytest.raises(InjectedFailure, match="after task replace"):
        lifecycle.record_pr_urls(
            "task-a",
            {"api": "https://example.test/pr/3"},
            now=NOW,
        )

    assert state.load().tasks["task-a"].pr_urls == {}
    assert items.get(item.id) == before_item


def test_record_pr_urls_hotfix_preserves_task_when_workitem_is_unreadable(
    lifecycle_stores,
) -> None:
    lifecycle, state, items = lifecycle_stores
    item = _item(items)
    task = _task(work_item_id=item.id)
    task.pr_urls = {}
    lifecycle.register_task(task, item.id, now=NOW)
    corrupt_workitem(state.state_dir, item.id)

    url = "https://example.test/pr/3"
    recorded = lifecycle.record_pr_urls(
        task.slug,
        {"api": url},
        now=NOW,
        allow_unreadable_workitem=True,
    )

    assert recorded.pr_urls == {"api": url}
    assert state.load().tasks[task.slug].pr_urls == {"api": url}

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from mship.core.persistence.migration_ownership import (
    apply_ownership_plan,
    plan_ownership,
)
from mship.core.state import Task, WorkspaceState
from mship.core.workitem import WorkItem


NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)


def _task(slug: str, owner: str | None) -> Task:
    return Task(
        slug=slug,
        description="Legacy task",
        phase="dev",
        created_at=NOW,
        affected_repos=[],
        branch=f"feat/{slug}",
        work_item_id=owner,
    )


def _item(item_id: str, slugs: list[str], **extra: object) -> WorkItem:
    return WorkItem(
        id=item_id,
        title="Same title",
        workspace="test",
        kind="bug",
        created_at=NOW,
        updated_at=NOW,
        spec_id="same-spec",
        task_slugs=slugs,
        **extra,
    )


def _snapshot(root: Path, name: str, *tasks: Task) -> Path:
    path = root / "backups" / name / "state.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            WorkspaceState(tasks={task.slug: task for task in tasks}).model_dump(
                mode="json"
            )
        )
    )
    return path


@pytest.mark.parametrize("reverse", [False, True])
def test_explicit_owner_does_not_depend_on_workitem_order_or_metadata(
    tmp_path: Path, reverse: bool
) -> None:
    state = WorkspaceState(tasks={"active": _task("active", "wi-canonical")})
    items = [
        _item(
            "wi-stale",
            ["sibling", "active", "sibling"],
            archived=True,
            plan_path="plans/keep.md",
            future_field={"keep": True},
        ),
        _item("wi-canonical", ["active"], thread_ids=["keep-thread"]),
    ]
    if reverse:
        items.reverse()
    original = [item.model_dump(mode="json") for item in items]

    plan = plan_ownership(tmp_path, state, items)
    actual = apply_ownership_plan(items, plan)

    assert not plan.conflicts
    expected = {item.id: item.model_dump(mode="json") for item in items}
    expected["wi-stale"]["task_slugs"] = ["sibling"]
    assert {item.id: item.model_dump(mode="json") for item in actual} == expected
    assert [item.model_dump(mode="json") for item in items] == original
    resolution = next(r for r in plan.resolutions if r.task_slug == "active")
    assert resolution.owner_id == "wi-canonical"
    assert resolution.removed_owner_ids == ("wi-stale",)


def test_removed_task_requires_consensus_not_newest_or_oldest_snapshot(
    tmp_path: Path,
) -> None:
    items = [_item("wi-a", ["closed"]), _item("wi-b", ["closed", "sibling"])]
    _snapshot(tmp_path, "old", _task("closed", "wi-a"))
    later = _snapshot(tmp_path, "new", _task("closed", "wi-b"))
    state = WorkspaceState()

    conflicted = plan_ownership(tmp_path, state, items)
    assert [c.task_slug for c in conflicted.conflicts] == ["closed"]
    assert len(conflicted.conflicts[0].evidence) == 2
    with pytest.raises(ValueError):
        apply_ownership_plan(items, conflicted)

    explicit = plan_ownership(
        tmp_path, state, items, owner_resolutions={"closed": "wi-b"}
    )
    assert not explicit.conflicts
    assert [item.task_slugs for item in apply_ownership_plan(items, explicit)] == [
        [],
        ["closed", "sibling"],
    ]
    assert (
        next(r for r in explicit.resolutions if r.task_slug == "closed").rule
        == "operator"
    )

    later.write_text(
        yaml.safe_dump(
            WorkspaceState(tasks={"closed": _task("closed", "wi-a")}).model_dump(
                mode="json"
            )
        )
    )
    agreed = plan_ownership(tmp_path, state, items)
    assert not agreed.conflicts
    assert [item.task_slugs for item in apply_ownership_plan(items, agreed)] == [
        ["closed"],
        ["sibling"],
    ]
    assert set(agreed.evidence_fingerprints) == {
        "backups/old/state.yaml",
        "backups/new/state.yaml",
    }


def test_invalid_owner_and_operator_conflicts_are_aggregated(tmp_path: Path) -> None:
    state = WorkspaceState(
        tasks={
            "missing": _task("missing", "wi-absent"),
            "owned": _task("owned", "wi-a"),
            "ambiguous": _task("ambiguous", None),
        }
    )
    items = [
        _item("wi-a", ["missing", "owned", "ambiguous"]),
        _item("wi-b", ["owned", "ambiguous"]),
    ]
    plan = plan_ownership(
        tmp_path,
        state,
        items,
        owner_resolutions={
            "missing": "wi-a",
            "owned": "wi-b",
            "unknown-slug": "wi-a",
        },
    )
    assert {c.task_slug for c in plan.conflicts} == {
        "missing",
        "owned",
        "ambiguous",
        "unknown-slug",
    }
    with pytest.raises(ValueError):
        apply_ownership_plan(items, plan)
    assert items[0].task_slugs == ["missing", "owned", "ambiguous"]
    assert items[1].task_slugs == ["owned", "ambiguous"]


def test_unreadable_history_never_masks_potentially_conflicting_owner(
    tmp_path: Path,
) -> None:
    _snapshot(tmp_path, "valid", _task("closed", "wi-a"))
    broken = _snapshot(tmp_path, "broken", _task("closed", "wi-b"))
    broken.write_text("tasks: [malformed]\n")
    items = [_item("wi-a", ["closed"]), _item("wi-b", ["closed"])]
    plan = plan_ownership(tmp_path, WorkspaceState(), items)
    assert [c.task_slug for c in plan.conflicts] == ["closed"]
    assert any("backups/broken/state.yaml" in e for e in plan.conflicts[0].evidence)


def test_external_history_symlink_is_not_ownership_authority(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    external = _snapshot(tmp_path / "outside", "one", _task("closed", "wi-a"))
    link = state_dir / "state.yaml.migrated-old"
    state_dir.mkdir()
    link.symlink_to(external)
    items = [_item("wi-a", ["closed"]), _item("wi-b", ["closed"])]
    plan = plan_ownership(state_dir, WorkspaceState(), items)
    assert [c.task_slug for c in plan.conflicts] == ["closed"]
    assert plan.evidence_fingerprints == {}

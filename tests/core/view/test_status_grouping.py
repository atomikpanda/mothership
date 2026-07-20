from datetime import datetime, timezone

from mship.core.state import Task, WorkspaceState
from mship.core.workitem import WorkItem
from mship.core.view.task_index import build_task_index
from mship.core.view.workitem_index import build_workitem_index
from mship.core.view.status_grouping import WorkItemTaskGroup, group_tasks_by_workitem


def _now():
    return datetime(2026, 7, 1, tzinfo=timezone.utc)


def _task(slug, phase):
    return Task(slug=slug, description=slug, phase=phase, created_at=_now(),
                affected_repos=["r"], branch=f"feat/{slug}", worktrees={})


def _fixture(tmp_path):
    tasks = {"a": _task("a", "dev"), "b": _task("b", "review"), "c": _task("c", "plan")}
    state = WorkspaceState(tasks=tasks)
    index = build_task_index(state, tmp_path)
    wi = WorkItem(id="wi-1", title="Overhaul", workspace="ws", kind="feature",
                  created_at=_now(), updated_at=_now(), task_slugs=["a", "b"])
    workitems = build_workitem_index([wi], {}, tasks, {})
    return index, workitems


def test_groups_linked_tasks_under_workitem(tmp_path):
    index, workitems = _fixture(tmp_path)
    groups = group_tasks_by_workitem(index, workitems)
    assert isinstance(groups[0], WorkItemTaskGroup)
    assert groups[0].work_item_id == "wi-1"
    assert {t.slug for t in groups[0].tasks} == {"a", "b"}
    assert groups[0].title == "Overhaul"
    assert groups[0].phase == "in_flight"  # a running task -> in_flight


def test_unlinked_tasks_fall_into_trailing_none_group(tmp_path):
    index, workitems = _fixture(tmp_path)
    groups = group_tasks_by_workitem(index, workitems)
    tail = groups[-1]
    assert tail.work_item_id is None
    assert [t.slug for t in tail.tasks] == ["c"]


def test_each_task_retains_its_own_phase(tmp_path):
    index, workitems = _fixture(tmp_path)
    groups = group_tasks_by_workitem(index, workitems)
    phases = {t.slug: t.phase for g in groups for t in g.tasks}
    assert phases == {"a": "dev", "b": "review", "c": "plan"}


def test_no_workitems_yields_single_ungrouped_bucket(tmp_path):
    index, _ = _fixture(tmp_path)
    groups = group_tasks_by_workitem(index, [])
    assert len(groups) == 1
    assert groups[0].work_item_id is None
    assert {t.slug for t in groups[0].tasks} == {"a", "b", "c"}

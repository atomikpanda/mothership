from datetime import datetime, timezone

from mship.core.spec import Spec
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.workitem_gate import GateResult, check_task_gate, log_hotfix
from mship.core.workitem_store import WorkItemStore


def _now():
    return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def _task(slug="t", work_item_id=None):
    return Task(slug=slug, description="d", phase="dev", created_at=_now(),
                affected_repos=["mothership"], branch="feat/t", work_item_id=work_item_id)


def test_no_work_item_id_is_not_ok(tmp_path):
    result = check_task_gate(_task(work_item_id=None), tmp_path)
    assert isinstance(result, GateResult)
    assert not result.ok
    assert "no WorkItem" in result.reason


def test_missing_work_item_is_not_ok(tmp_path):
    result = check_task_gate(_task(work_item_id="wi-nope"), tmp_path)
    assert not result.ok
    assert "wi-nope" in result.reason


def test_bug_work_item_ok_without_spec(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="fix it", kind="bug", workspace="ws", now=_now())
    result = check_task_gate(_task(work_item_id=wi.id), tmp_path)
    assert result.ok
    assert result.reason is None


def test_feature_work_item_without_approved_spec_is_not_ok(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=_now())
    result = check_task_gate(_task(work_item_id=wi.id), tmp_path)
    assert not result.ok
    assert "approved spec" in result.reason


def test_feature_work_item_with_approved_spec_via_spec_id_is_ok(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    specs = SpecStore(tmp_path / "specs")
    specs.save(Spec(id="spec-1", title="Spec", status="approved",
                    created_at=_now(), updated_at=_now()))
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=_now())
    items.link_spec(wi.id, "spec-1", now=_now())
    result = check_task_gate(_task(work_item_id=wi.id), tmp_path)
    assert result.ok


def test_feature_work_item_with_unapproved_spec_via_spec_id_is_not_ok(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    specs = SpecStore(tmp_path / "specs")
    specs.save(Spec(id="spec-1", title="Spec", status="drafting",
                    created_at=_now(), updated_at=_now()))
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=_now())
    items.link_spec(wi.id, "spec-1", now=_now())
    result = check_task_gate(_task(work_item_id=wi.id), tmp_path)
    assert not result.ok


def test_feature_work_item_with_approved_spec_via_task_slug_fallback_is_ok(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    specs = SpecStore(tmp_path / "specs")
    specs.save(Spec(id="spec-1", title="Spec", status="dispatched",
                    created_at=_now(), updated_at=_now(), task_slug="t"))
    wi = items.create(title="add thing", kind="feature", workspace="ws", now=_now())
    # No spec_id link on the WorkItem itself — fallback must scan specs by task_slug.
    result = check_task_gate(_task(slug="t", work_item_id=wi.id), tmp_path)
    assert result.ok


def test_add_task_sets_reverse_link_on_task(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    state = StateManager(tmp_path / ".mothership")
    state.save(WorkspaceState(tasks={"t": Task(
        slug="t", description="d", phase="dev", created_at=_now(),
        affected_repos=["mothership"], branch="feat/t")}))
    wi = items.create(title="add thing", kind="chore", workspace="ws", now=_now())

    items.add_task(wi.id, "t", now=_now(), state=state)

    assert state.load().tasks["t"].work_item_id == wi.id
    assert items.get(wi.id).task_slugs == ["t"]


def test_add_task_without_state_does_not_write_reverse_link(tmp_path):
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    state = StateManager(tmp_path / ".mothership")
    state.save(WorkspaceState(tasks={"t": Task(
        slug="t", description="d", phase="dev", created_at=_now(),
        affected_repos=["mothership"], branch="feat/t")}))
    wi = items.create(title="add thing", kind="chore", workspace="ws", now=_now())

    items.add_task(wi.id, "t", now=_now())

    assert state.load().tasks["t"].work_item_id is None


def test_log_hotfix_appends_bypass_log(tmp_path):
    (tmp_path / ".mothership").mkdir()
    log_hotfix(tmp_path, "dev", "some-task")
    log_path = tmp_path / ".mothership" / "bypass-log.jsonl"
    assert log_path.is_file()
    import json
    line = json.loads(log_path.read_text().splitlines()[-1])
    assert line["reason"] == "hotfix"
    assert line["op"] == "dev"
    assert line["branch"] == "some-task"

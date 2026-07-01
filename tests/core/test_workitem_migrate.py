from datetime import datetime, timezone

from mship.core.spec import Spec
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.message_store import MessageStore
from mship.core.workitem_store import WorkItemStore
from mship.core.workitem_migrate import wrap_existing


def _now():
    return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def _setup(tmp_path):
    specs = SpecStore(tmp_path / "specs")
    state = StateManager(tmp_path / ".mothership")
    msgs = MessageStore(tmp_path / ".mothership" / "messages")
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    return specs, state, msgs, items


def test_spec_with_task_becomes_one_feature_item(tmp_path):
    specs, state, msgs, items = _setup(tmp_path)
    specs.save(Spec(id="alpha", title="Alpha", status="approved",
                    created_at=_now(), updated_at=_now(), task_slug="alpha"))
    state.save(WorkspaceState(tasks={"alpha": Task(
        slug="alpha", description="d", phase="dev", created_at=_now(),
        affected_repos=["mothership"], branch="b", spec_id="alpha")}))

    wrap_existing(items, specs, state, msgs, now=_now())

    created = items.list()
    assert len(created) == 1
    wi = created[0]
    assert wi.kind == "feature" and wi.spec_id == "alpha" and wi.task_slugs == ["alpha"]
    assert specs.find_by_id("alpha").work_item_id == wi.id
    assert state.load().tasks["alpha"].work_item_id == wi.id


def test_orphan_task_becomes_chore_item(tmp_path):
    specs, state, msgs, items = _setup(tmp_path)
    state.save(WorkspaceState(tasks={"bugfix": Task(
        slug="bugfix", description="d", phase="dev", created_at=_now(),
        affected_repos=["mothership"], branch="b")}))

    wrap_existing(items, specs, state, msgs, now=_now())

    wi = items.list()[0]
    assert wi.kind == "chore" and wi.task_slugs == ["bugfix"] and wi.spec_id is None


def test_idempotent(tmp_path):
    specs, state, msgs, items = _setup(tmp_path)
    specs.save(Spec(id="alpha", title="Alpha", status="drafting",
                    created_at=_now(), updated_at=_now()))
    wrap_existing(items, specs, state, msgs, now=_now())
    wrap_existing(items, specs, state, msgs, now=_now())
    assert len(items.list()) == 1

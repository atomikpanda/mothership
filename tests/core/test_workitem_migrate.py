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

    wrap_existing(items, specs, state, msgs, now=_now(), workspace="testws")

    created = items.list()
    assert len(created) == 1
    wi = created[0]
    assert wi.kind == "feature" and wi.spec_id == "alpha" and wi.task_slugs == ["alpha"]
    assert wi.workspace == "testws"
    assert specs.find_by_id("alpha").work_item_id == wi.id
    assert state.load().tasks["alpha"].work_item_id == wi.id


def test_orphan_task_becomes_chore_item(tmp_path):
    specs, state, msgs, items = _setup(tmp_path)
    state.save(WorkspaceState(tasks={"bugfix": Task(
        slug="bugfix", description="d", phase="dev", created_at=_now(),
        affected_repos=["mothership"], branch="b")}))

    wrap_existing(items, specs, state, msgs, now=_now(), workspace="testws")

    wi = items.list()[0]
    assert wi.kind == "chore" and wi.task_slugs == ["bugfix"] and wi.spec_id is None
    assert wi.workspace == "testws"


def test_idempotent(tmp_path):
    specs, state, msgs, items = _setup(tmp_path)
    specs.save(Spec(id="alpha", title="Alpha", status="drafting",
                    created_at=_now(), updated_at=_now()))
    wrap_existing(items, specs, state, msgs, now=_now(), workspace="testws")
    wrap_existing(items, specs, state, msgs, now=_now(), workspace="testws")
    assert len(items.list()) == 1


def test_orphan_task_with_approved_spec_becomes_feature_item(tmp_path):
    # spec has no task_slug back-reference, so pass 1 wraps it but does not
    # attach "orphan" — the task's own spec_id must still resolve it in pass 2.
    specs, state, msgs, items = _setup(tmp_path)
    specs.save(Spec(id="alpha", title="Alpha", status="approved",
                    created_at=_now(), updated_at=_now()))
    state.save(WorkspaceState(tasks={"orphan": Task(
        slug="orphan", description="d", phase="dev", created_at=_now(),
        affected_repos=["mothership"], branch="b", spec_id="alpha")}))

    wrap_existing(items, specs, state, msgs, now=_now(), workspace="testws")

    task_wi = next(w for w in items.list() if "orphan" in w.task_slugs)
    assert task_wi.kind == "feature"
    assert task_wi.spec_id == "alpha"
    assert state.load().tasks["orphan"].work_item_id == task_wi.id


def test_orphan_task_with_unapproved_spec_stays_chore_item(tmp_path):
    specs, state, msgs, items = _setup(tmp_path)
    specs.save(Spec(id="beta", title="Beta", status="drafting",
                    created_at=_now(), updated_at=_now()))
    state.save(WorkspaceState(tasks={"orphan2": Task(
        slug="orphan2", description="d", phase="dev", created_at=_now(),
        affected_repos=["mothership"], branch="b", spec_id="beta")}))

    wrap_existing(items, specs, state, msgs, now=_now(), workspace="testws")

    task_wi = next(w for w in items.list() if "orphan2" in w.task_slugs)
    assert task_wi.kind == "chore"
    assert task_wi.spec_id is None
    assert state.load().tasks["orphan2"].work_item_id == task_wi.id


def test_pass2_feature_link_is_idempotent(tmp_path):
    specs, state, msgs, items = _setup(tmp_path)
    specs.save(Spec(id="alpha", title="Alpha", status="approved",
                    created_at=_now(), updated_at=_now()))
    state.save(WorkspaceState(tasks={"orphan": Task(
        slug="orphan", description="d", phase="dev", created_at=_now(),
        affected_repos=["mothership"], branch="b", spec_id="alpha")}))

    wrap_existing(items, specs, state, msgs, now=_now(), workspace="testws")
    count_after_first = len(items.list())
    wrap_existing(items, specs, state, msgs, now=_now(), workspace="testws")
    count_after_second = len(items.list())

    assert count_after_first == count_after_second


def test_thread_attaches_via_spec(tmp_path):
    specs, state, msgs, items = _setup(tmp_path)
    specs.save(Spec(id="alpha", title="Alpha", status="approved",
                    created_at=_now(), updated_at=_now(), task_slug="alpha"))
    state.save(WorkspaceState(tasks={"alpha": Task(
        slug="alpha", description="d", phase="dev", created_at=_now(),
        affected_repos=["mothership"], branch="b", spec_id="alpha")}))
    thread = msgs.create_thread(subject="s", text="hi", now=_now())
    msgs.link_spec(thread.id, "alpha", now=_now())

    wrap_existing(items, specs, state, msgs, now=_now(), workspace="testws")

    wi = items.list()[0]
    assert thread.id in wi.thread_ids

    # Re-running must not duplicate the thread id.
    wrap_existing(items, specs, state, msgs, now=_now(), workspace="testws")
    wi_again = items.list()[0]
    assert wi_again.thread_ids.count(thread.id) == 1


def test_thread_attaches_via_task_slug(tmp_path):
    specs, state, msgs, items = _setup(tmp_path)
    state.save(WorkspaceState(tasks={"bugfix": Task(
        slug="bugfix", description="d", phase="dev", created_at=_now(),
        affected_repos=["mothership"], branch="b")}))
    thread = msgs.create_thread(subject="s", text="hi", now=_now(), task_slug="bugfix")

    wrap_existing(items, specs, state, msgs, now=_now(), workspace="testws")

    wi = items.list()[0]
    assert thread.id in wi.thread_ids

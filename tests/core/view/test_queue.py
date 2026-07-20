from datetime import datetime, timezone

from mship.core.spec import Spec
from mship.core.state import Task
from mship.core.workitem import WorkItem
from mship.core.view.workitem_index import build_workitem_index
from mship.core.view.queue import assemble_queue


def _now():
    return datetime(2026, 7, 1, tzinfo=timezone.utc)


def _summary(spec=None, tasks=()):
    wi = WorkItem(
        id="wi-1", title="Overhaul", workspace="ws", kind="feature",
        created_at=_now(), updated_at=_now(),
        spec_id=(spec.id if spec else None),
        task_slugs=[t.slug for t in tasks],
    )
    return build_workitem_index(
        [wi],
        {spec.id: spec} if spec else {},
        {t.slug: t for t in tasks},
        {},
    )[0]


def _tasks_by_slug(*tasks):
    return {t.slug: t for t in tasks}


def test_needs_review_spec_becomes_a_spec_queue_item():
    spec = Spec(id="spec-1", title="Overhaul spec", status="needs_review",
                created_at=_now(), updated_at=_now(), body="b\n")
    summary = _summary(spec=spec)
    items = assemble_queue([summary], {})
    assert [i.kind for i in items] == ["spec-needs-review"]
    it = items[0]
    assert it.key == "spec:wi-1"
    assert it.spec_id == "spec-1"
    assert it.work_item_id == "wi-1"
    assert it.work_item_title == "Overhaul"
    assert it.workspace == "ws"


def test_approved_spec_is_not_in_queue():
    spec = Spec(id="spec-1", title="Overhaul spec", status="approved",
                created_at=_now(), updated_at=_now(), body="b\n")
    items = assemble_queue([_summary(spec=spec)], {})
    assert items == []


def test_blocked_task_becomes_a_blocked_queue_item():
    task = Task(slug="a", description="d", phase="dev", created_at=_now(),
                affected_repos=["r"], branch="feat/a",
                blocked_reason="waiting on API key")
    summary = _summary(tasks=[task])
    items = assemble_queue([summary], _tasks_by_slug(task))
    assert [i.kind for i in items] == ["blocked-task"]
    it = items[0]
    assert it.key == "block:a"
    assert it.task_slug == "a"
    assert it.blocked_reason == "waiting on API key"
    assert it.work_item_id == "wi-1"


def test_unblocked_task_is_not_in_queue():
    task = Task(slug="a", description="d", phase="dev", created_at=_now(),
                affected_repos=["r"], branch="feat/a")
    summary = _summary(tasks=[task])
    assert assemble_queue([summary], _tasks_by_slug(task)) == []


def test_recorded_pr_urls_become_pr_queue_items():
    task = Task(slug="a", description="d", phase="review", created_at=_now(),
                affected_repos=["r"], branch="feat/a",
                pr_urls={"r": "https://gh/pr/1"}, finished_at=_now())
    summary = _summary(tasks=[task])
    items = assemble_queue([summary], _tasks_by_slug(task))
    assert [i.kind for i in items] == ["pr-awaiting"]
    it = items[0]
    assert it.key == "pr:a:r"
    assert it.repo == "r"
    assert it.pr_url == "https://gh/pr/1"
    assert it.task_slug == "a"


def test_done_workitem_prs_are_excluded():
    # A closed/merged item derives phase "done" (terminal spec status). Its
    # recorded pr_urls must NOT show as awaiting action — no live gh call needed.
    task = Task(slug="a", description="d", phase="review", created_at=_now(),
                affected_repos=["r"], branch="feat/a",
                pr_urls={"r": "https://gh/pr/1"}, finished_at=_now())
    spec = Spec(id="spec-1", title="done spec", status="implemented",
                created_at=_now(), updated_at=_now(), body="b\n")
    wi = WorkItem(id="wi-1", title="Overhaul", workspace="ws", kind="feature",
                  created_at=_now(), updated_at=_now(), spec_id="spec-1",
                  task_slugs=["a"])
    summary = build_workitem_index([wi], {"spec-1": spec}, {"a": task}, {})[0]
    assert summary.phase == "done"
    assert assemble_queue([summary], _tasks_by_slug(task)) == []


def test_queue_order_is_specs_then_blocked_then_prs():
    spec = Spec(id="spec-1", title="Overhaul spec", status="needs_review",
                created_at=_now(), updated_at=_now(), body="b\n")
    blocked = Task(slug="a", description="d", phase="dev", created_at=_now(),
                   affected_repos=["r"], branch="feat/a", blocked_reason="x")
    pr = Task(slug="b", description="d", phase="review", created_at=_now(),
              affected_repos=["r"], branch="feat/b",
              pr_urls={"r": "https://gh/pr/9"}, finished_at=_now())
    wi = WorkItem(id="wi-1", title="Overhaul", workspace="ws", kind="feature",
                  created_at=_now(), updated_at=_now(), spec_id="spec-1",
                  task_slugs=["a", "b"])
    summary = build_workitem_index(
        [wi], {"spec-1": spec}, {"a": blocked, "b": pr}, {})[0]
    items = assemble_queue([summary], _tasks_by_slug(blocked, pr))
    assert [i.kind for i in items] == [
        "spec-needs-review", "blocked-task", "pr-awaiting"]

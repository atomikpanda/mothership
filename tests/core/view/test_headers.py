from datetime import datetime, timezone

from mship.core.workitem import WorkItem
from mship.core.view.workitem_index import build_workitem_index
from mship.core.view.headers import header_for_spec, header_for_task


def _now():
    return datetime(2026, 7, 1, tzinfo=timezone.utc)


def _index(**kw):
    base = dict(id="wi-1", title="View overhaul", workspace="ws", kind="feature",
                created_at=_now(), updated_at=_now())
    base.update(kw)
    return build_workitem_index([WorkItem(**base)], {}, {}, {})


def test_header_for_task_includes_workitem_and_phases():
    workitems = _index(task_slugs=["demo"])
    line = header_for_task("demo", "dev", workitems)
    assert "wi-1" in line
    assert "View overhaul" in line
    # WorkItem derived phase (no children -> inbox) and the task's own phase.
    assert "[inbox]" in line
    assert "demo" in line and "[dev]" in line


def test_header_for_task_none_when_task_unlinked():
    workitems = _index(task_slugs=["other"])
    assert header_for_task("demo", "dev", workitems) is None


def test_header_for_task_omits_task_phase_when_none():
    workitems = _index(task_slugs=["demo"])
    line = header_for_task("demo", None, workitems)
    assert "wi-1" in line
    assert "task demo" not in line


def test_header_for_spec_resolves_by_spec_id():
    workitems = _index(spec_id="spec-9")
    line = header_for_spec("spec-9", workitems)
    assert "wi-1" in line and "View overhaul" in line and "[inbox]" in line


def test_header_for_spec_none_when_unlinked():
    workitems = _index(spec_id="spec-9")
    assert header_for_spec("other-spec", workitems) is None

# tests/core/test_workitem.py
from datetime import datetime, timezone

from mship.core.workitem import WorkItem, ExternalLink, PHASE_ORDER


def _now():
    return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def test_workitem_defaults_and_roundtrip():
    wi = WorkItem(id="wi-1", title="Make capture conversational", workspace="mothership",
                  kind="feature", created_at=_now(), updated_at=_now())
    assert wi.spec_id is None
    assert wi.task_slugs == [] and wi.thread_ids == [] and wi.external_links == []
    assert wi.phase_override is None
    restored = WorkItem.model_validate_json(wi.model_dump_json())
    assert restored == wi


def test_external_link_and_links_list():
    link = ExternalLink(provider="github", url="https://github.com/atomikpanda/mothership/issues/249",
                        title="MOS-196")
    wi = WorkItem(id="wi-2", title="t", workspace="ws", kind="bug",
                  created_at=_now(), updated_at=_now(), external_links=[link])
    assert wi.external_links[0].provider == "github"


def test_phase_order_is_the_pipeline():
    assert PHASE_ORDER == ("inbox", "shaping", "ready", "in_flight", "review", "done")


def test_workitem_unattended_defaults_false_and_roundtrips():
    from mship.core.workitem import WorkItem
    from datetime import datetime, timezone
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    wi = WorkItem(id="wi-1", title="t", workspace="ws", kind="feature",
                  created_at=now, updated_at=now)
    assert wi.unattended is False
    dumped = wi.model_dump_json()
    assert WorkItem.model_validate_json(dumped).unattended is False
    wi2 = wi.model_copy(update={"unattended": True})
    assert WorkItem.model_validate_json(wi2.model_dump_json()).unattended is True


def test_workitem_archived_defaults_false_and_roundtrips():
    now = _now()
    wi = WorkItem(id="wi-1", title="t", workspace="ws", kind="feature",
                  created_at=now, updated_at=now)
    assert wi.archived is False
    dumped = wi.model_dump_json()
    assert WorkItem.model_validate_json(dumped).archived is False
    wi2 = wi.model_copy(update={"archived": True})
    assert WorkItem.model_validate_json(wi2.model_dump_json()).archived is True

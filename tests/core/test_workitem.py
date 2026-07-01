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

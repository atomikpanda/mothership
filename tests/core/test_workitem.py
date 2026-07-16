# tests/core/test_workitem.py
import json
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


def test_unknown_future_field_survives_roundtrip():
    # #342 root cause: a WorkItem JSON is read-modify-written by many code paths, including a
    # possibly-OLDER binary (e.g. a stale installed `mship serve`) whose schema predates a newer
    # field. With the default extra="ignore" such a binary silently drops the newer field on re-save
    # (that's how `plan_path` got nulled while the older `spec_id` survived). extra="allow" must
    # round-trip fields this schema doesn't know about.
    raw = {
        "id": "wi-9", "title": "t", "workspace": "ws", "kind": "feature",
        "created_at": _now().isoformat(), "updated_at": _now().isoformat(),
        "spec_id": "spec-1",
        "a_field_added_by_a_newer_binary": "keep-me",
    }
    wi = WorkItem.model_validate(raw)
    dumped = json.loads(wi.model_dump_json())
    assert dumped["a_field_added_by_a_newer_binary"] == "keep-me"  # unknown field preserved
    assert dumped["spec_id"] == "spec-1"                            # known fields intact


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


def test_workitem_plan_path_defaults_none_and_roundtrips():
    now = _now()
    wi = WorkItem(id="wi-1", title="t", workspace="ws", kind="feature",
                  created_at=now, updated_at=now)
    assert wi.plan_path is None
    dumped = wi.model_dump_json()
    assert WorkItem.model_validate_json(dumped).plan_path is None
    wi2 = wi.model_copy(update={"plan_path": "docs/plans/x.md"})
    assert WorkItem.model_validate_json(wi2.model_dump_json()).plan_path == "docs/plans/x.md"


def test_workitem_archived_defaults_false_and_roundtrips():
    now = _now()
    wi = WorkItem(id="wi-1", title="t", workspace="ws", kind="feature",
                  created_at=now, updated_at=now)
    assert wi.archived is False
    dumped = wi.model_dump_json()
    assert WorkItem.model_validate_json(dumped).archived is False
    wi2 = wi.model_copy(update={"archived": True})
    assert WorkItem.model_validate_json(wi2.model_dump_json()).archived is True

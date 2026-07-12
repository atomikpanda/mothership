from datetime import datetime, timezone

from mship.core.spec import Spec


def test_spec_work_item_id_defaults_none_and_legacy_loads():
    # legacy JSON without the field still validates (back-compat)
    legacy = ('{"id":"s","title":"t","status":"draft",'
              '"created_at":"2026-06-30T12:00:00+00:00","updated_at":"2026-06-30T12:00:00+00:00"}')
    spec = Spec.model_validate_json(legacy)
    assert spec.work_item_id is None
    spec.work_item_id = "wi-1"
    assert Spec.model_validate_json(spec.model_dump_json()).work_item_id == "wi-1"

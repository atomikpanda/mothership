from mship.core.spec import Spec


def test_spec_clarification_reason_defaults_none_and_legacy_loads():
    # legacy JSON without the field still validates (back-compat)
    legacy = ('{"id":"s","title":"t","status":"draft",'
              '"created_at":"2026-06-30T12:00:00+00:00","updated_at":"2026-06-30T12:00:00+00:00"}')
    spec = Spec.model_validate_json(legacy)
    assert spec.clarification_reason is None
    spec.clarification_reason = "tighten scope"
    assert Spec.model_validate_json(spec.model_dump_json()).clarification_reason == "tighten scope"

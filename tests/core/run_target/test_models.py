from pydantic import ValidationError
import pytest

from mship.core.run_target.models import (
    BackendConfig,
    HostRequirements,
    RunProfile,
    TargetCandidate,
    TargetSelectionError,
    profile_revision,
)


def test_profile_models_reject_unknown_and_non_json_options():
    with pytest.raises(ValidationError):
        RunProfile.model_validate(
            {"backend": "flutter", "hosts": {"roles": ["mobile"]}, "options": {}, "raw_id": "usb-1"}
        )
    with pytest.raises(ValidationError):
        BackendConfig.model_validate({"discover_task": "discover", "operations": {}, "shell": "adb"})
    with pytest.raises(ValidationError):
        RunProfile(backend="flutter", hosts=HostRequirements(roles=("mobile",)), options={"bad": {1, 2}})


def test_candidate_rejects_unready_without_reason_and_non_integer_rank():
    common = dict(
        target_key="private", label="Phone", tags=(), capabilities=("run",),
        remediation=None, preparation=(), binding={},
    )
    with pytest.raises(ValidationError):
        TargetCandidate(**common, ready=False, reason=None, rank=(1,))
    with pytest.raises(ValidationError):
        TargetCandidate(**common, ready=True, reason="busy", rank=(1,))
    with pytest.raises(ValidationError):
        TargetCandidate(**common, ready=True, reason=None, rank=(True,))


def test_profile_revision_changes_for_backend_or_prepared_source_not_friendly_name():
    profile = RunProfile(backend="flutter", hosts=HostRequirements(roles=("mobile",)), options={"engine": "ios"})
    backend = BackendConfig(discover_task="discover", operations={"run": "run"})
    original = profile_revision(profile, backend, prepared_source_revision="abc123")
    assert original != profile_revision(profile, backend, prepared_source_revision="def456")
    assert original != profile_revision(
        profile, BackendConfig(discover_task="discover", operations={"run": "launch"}), prepared_source_revision="abc123"
    )


def test_selection_error_exposes_only_declared_safe_details():
    error = TargetSelectionError("backend_protocol", "backend emitted invalid discovery data", ("rank_schema",))
    assert error.code == "backend_protocol"
    assert error.details == ("rank_schema",)
    assert str(error) == "backend emitted invalid discovery data"

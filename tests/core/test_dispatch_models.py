import pytest

from mship.core.dispatch_models import resolve_model


def test_flag_wins():
    assert resolve_model("implementer", flag="opus", configured={"implementer": "sonnet"}) == "opus"


def test_config_beats_builtin():
    assert resolve_model("reviewer", flag=None, configured={"reviewer": "haiku"}) == "haiku"


@pytest.mark.parametrize("mode", ["implementer", "standalone", "reviewer"])
def test_builtin_defaults_inherit_harness_model(mode):
    assert resolve_model(mode, flag=None, configured=None) == "inherit"


def test_operator_model_value_is_opaque_and_verbatim():
    value = "vendor/custom-tier:2026-08"
    assert resolve_model("reviewer", flag=None, configured={"reviewer": value}) == value


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        resolve_model("juggler", flag=None, configured=None)


def test_config_field_roundtrip():
    from mship.core.config import WorkspaceConfig

    cfg = WorkspaceConfig(workspace="w", dispatch_models={"implementer": "sonnet"})
    assert cfg.dispatch_models == {"implementer": "sonnet"}
    assert WorkspaceConfig(workspace="w").dispatch_models is None

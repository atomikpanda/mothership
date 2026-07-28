import pytest

from mship.core.dispatch_models import resolve_model, BUILTIN_MODEL_DEFAULTS


def test_flag_wins():
    assert resolve_model("implementer", flag="opus", configured={"implementer": "sonnet"}) == "opus"


def test_config_beats_builtin():
    assert resolve_model("reviewer", flag=None, configured={"reviewer": "haiku"}) == "haiku"


def test_builtin_default_per_mode():
    assert resolve_model("implementer", flag=None, configured=None) == BUILTIN_MODEL_DEFAULTS["implementer"]
    assert resolve_model("reviewer", flag=None, configured=None) == BUILTIN_MODEL_DEFAULTS["reviewer"]


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        resolve_model("juggler", flag=None, configured=None)


def test_config_field_roundtrip():
    from mship.core.config import WorkspaceConfig

    cfg = WorkspaceConfig(workspace="w", dispatch_models={"implementer": "sonnet"})
    assert cfg.dispatch_models == {"implementer": "sonnet"}
    assert WorkspaceConfig(workspace="w").dispatch_models is None

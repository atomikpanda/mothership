from pathlib import Path

import pytest
from pydantic import ValidationError

from mship.core.config import ConfigLoader, WorkspaceConfig


def test_spec_storage_defaults_to_committed():
    cfg = WorkspaceConfig(workspace="demo")
    assert cfg.spec_storage == "committed"


def test_spec_storage_accepts_each_mode():
    for mode in ("committed", "local", "encrypted"):
        assert WorkspaceConfig(workspace="demo", spec_storage=mode).spec_storage == mode


def test_invalid_spec_storage_value_rejected_by_model():
    with pytest.raises(ValidationError):
        WorkspaceConfig(workspace="demo", spec_storage="public")


def test_invalid_spec_storage_fails_at_config_load(tmp_path: Path):
    (tmp_path / "mothership.yaml").write_text(
        "workspace: demo\nspec_storage: public\n"
    )
    with pytest.raises(ValidationError):
        ConfigLoader.load(tmp_path / "mothership.yaml", require_paths=False)

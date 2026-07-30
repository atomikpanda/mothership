from pathlib import Path

import pytest
from pydantic import ValidationError

from mship.core.config import ConfigLoader, WorkspaceConfig


def test_assumption_storage_defaults_to_committed():
    cfg = WorkspaceConfig(workspace="demo")
    assert cfg.assumption_storage == "committed"


def test_assumption_storage_accepts_each_mode():
    for mode in ("committed", "local", "encrypted"):
        assert (
            WorkspaceConfig(workspace="demo", assumption_storage=mode).assumption_storage
            == mode
        )


def test_invalid_assumption_storage_value_rejected_by_model():
    with pytest.raises(ValidationError):
        WorkspaceConfig(workspace="demo", assumption_storage="public")


def test_invalid_assumption_storage_fails_at_config_load(tmp_path: Path):
    (tmp_path / "mothership.yaml").write_text(
        "workspace: demo\nassumption_storage: public\n"
    )
    with pytest.raises(ValidationError):
        ConfigLoader.load(tmp_path / "mothership.yaml", require_paths=False)

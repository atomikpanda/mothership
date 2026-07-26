from pathlib import Path

import pytest
from pydantic import ValidationError

from mship.core.config import ConfigLoader, WorkspaceConfig


def _write(tmp_path: Path, contents: str) -> Path:
    p = tmp_path / "mothership.yaml"
    p.write_text(contents)
    return p


def test_evidence_storage_defaults_to_none():
    cfg = WorkspaceConfig(workspace="demo")
    assert cfg.evidence_storage is None


def test_evidence_storage_accepts_each_mode():
    for mode in ("published", "local", "encrypted"):
        assert (
            WorkspaceConfig(workspace="demo", evidence_storage=mode).evidence_storage
            == mode
        )


def test_invalid_evidence_storage_value_rejected_by_model():
    with pytest.raises(ValidationError):
        WorkspaceConfig(workspace="demo", evidence_storage="public")


def test_spec_storages_own_word_is_not_an_evidence_mode():
    """`committed` belongs to spec_storage; evidence calls the same exposure
    `published`. Accepting both spellings would mean two names for one mode."""
    with pytest.raises(ValidationError):
        WorkspaceConfig(workspace="demo", evidence_storage="committed")


def test_invalid_evidence_storage_fails_at_config_load(tmp_path: Path):
    (tmp_path / "mothership.yaml").write_text(
        "workspace: demo\nevidence_storage: public\n"
    )
    with pytest.raises(ValidationError):
        ConfigLoader.load(tmp_path / "mothership.yaml", require_paths=False)


def test_evidence_more_exposed_than_spec_fails_at_config_load(tmp_path: Path):
    p = _write(tmp_path, (
        "workspace: w\nrepos: {}\n"
        "spec_storage: encrypted\nevidence_storage: published\n"
    ))
    with pytest.raises(Exception) as e:
        ConfigLoader.load(p, require_paths=False)
    msg = str(e.value)
    assert "evidence_storage" in msg and "spec_storage" in msg


def test_local_spec_with_published_evidence_fails_at_config_load(tmp_path: Path):
    p = _write(tmp_path, (
        "workspace: w\nrepos: {}\n"
        "spec_storage: local\nevidence_storage: published\n"
    ))
    with pytest.raises(Exception):
        ConfigLoader.load(p, require_paths=False)


def test_equal_or_less_exposed_evidence_loads_fine(tmp_path: Path):
    for spec_mode, ev_mode in (
        ("committed", "local"),
        ("encrypted", "local"),
        ("encrypted", "encrypted"),
        # The mapped-equal case: `committed` specs, `published` evidence.
        ("committed", "published"),
    ):
        p = _write(tmp_path, (
            f"workspace: w\nrepos: {{}}\n"
            f"spec_storage: {spec_mode}\nevidence_storage: {ev_mode}\n"
        ))
        ConfigLoader.load(p, require_paths=False)

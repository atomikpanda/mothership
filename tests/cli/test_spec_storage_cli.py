import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


@pytest.fixture
def encrypted_workspace(tmp_path: Path):
    (tmp_path / "mothership.yaml").write_text(
        "workspace: demo\nspec_storage: encrypted\n"
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(state_dir)
    yield tmp_path
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def test_spec_new_under_encrypted_writes_ciphertext(encrypted_workspace: Path):
    res = runner.invoke(app, ["--json", "spec", "new", "--title", "Hidden plan", "--id", "hidden-plan"])
    assert res.exit_code == 0, res.output
    enc = list((encrypted_workspace / "specs").glob("*.md.enc"))
    assert len(enc) == 1
    assert b"Hidden plan" not in enc[0].read_bytes()
    # No plaintext committed path exists.
    assert list((encrypted_workspace / "specs").glob("*.md")) == []


def test_spec_show_decrypts_with_key(encrypted_workspace: Path):
    runner.invoke(app, ["spec", "new", "--title", "Hidden plan", "--id", "hidden-plan"])
    res = runner.invoke(app, ["--json", "spec", "show", "hidden-plan"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["title"] == "Hidden plan"


def test_spec_list_under_encrypted(encrypted_workspace: Path):
    runner.invoke(app, ["spec", "new", "--title", "Hidden plan", "--id", "hidden-plan"])
    res = runner.invoke(app, ["--json", "spec", "list"])
    assert res.exit_code == 0, res.output
    ids = [s["id"] for s in json.loads(res.output)["specs"]]
    assert "hidden-plan" in ids


def test_spec_validate_under_encrypted(encrypted_workspace: Path):
    """`validate` reads the spec file directly today; under encrypted mode it must
    still decode via the storage layer, not choke on ciphertext."""
    runner.invoke(app, [
        "spec", "new", "--title", "Hidden plan", "--id", "hidden-plan",
    ])
    # Give it canonical body sections so validate has something to check.
    res = runner.invoke(app, ["--json", "spec", "validate", "hidden-plan"])
    # A freshly-scaffolded spec may be missing body sections; the point is that
    # validate found + decoded the ENCRYPTED spec rather than reporting "no spec
    # file" (which is what a raw *.md glob would do against a *.md.enc store).
    assert "No spec file for id" not in res.output

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from mship.cli import app

runner = CliRunner()


@pytest.fixture
def init_workspace(tmp_path: Path) -> Path:
    for name in ["shared", "auth-service"]:
        d = tmp_path / name
        d.mkdir()
        (d / ".git").mkdir()
        (d / "Taskfile.yml").write_text("version: '3'")
    return tmp_path


def test_init_non_interactive_with_cwd(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    result = runner.invoke(app, [
        "init",
        "--name", "test-platform",
        "--repo", "./shared:library",
        "--repo", "./auth-service:service:shared",
    ])
    assert result.exit_code == 0, result.output
    config_path = init_workspace / "mothership.yaml"
    assert config_path.exists()
    with open(config_path) as f:
        data = yaml.safe_load(f)
    assert data["workspace"] == "test-platform"
    assert "shared" in data["repos"]
    assert data["repos"]["shared"]["type"] == "library"
    assert data["repos"]["auth-service"]["type"] == "service"
    assert data["repos"]["auth-service"]["depends_on"] == ["shared"]


def test_init_detect(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    result = runner.invoke(app, [
        "init",
        "--name", "test-platform",
        "--detect",
    ])
    assert result.exit_code == 0, result.output
    config_path = init_workspace / "mothership.yaml"
    assert config_path.exists()
    with open(config_path) as f:
        data = yaml.safe_load(f)
    assert "shared" in data["repos"]
    assert "auth-service" in data["repos"]


def test_init_already_exists(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    (init_workspace / "mothership.yaml").write_text("workspace: existing")
    result = runner.invoke(app, [
        "init",
        "--name", "test",
        "--repo", "./shared:library",
    ])
    assert result.exit_code != 0 or "already exists" in result.output.lower()


def test_init_force_overwrite(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    (init_workspace / "mothership.yaml").write_text("workspace: existing")
    result = runner.invoke(app, [
        "init",
        "--name", "test",
        "--repo", "./shared:library",
        "--force",
    ])
    assert result.exit_code == 0, result.output


def test_init_env_runner(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    result = runner.invoke(app, [
        "init",
        "--name", "test",
        "--repo", "./shared:library",
        "--env-runner", "dotenvx run --",
    ])
    assert result.exit_code == 0, result.output
    with open(init_workspace / "mothership.yaml") as f:
        data = yaml.safe_load(f)
    assert data["env_runner"] == "dotenvx run --"


def test_init_scaffold_taskfiles(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    no_taskfile = init_workspace / "new-repo"
    no_taskfile.mkdir()
    (no_taskfile / ".git").mkdir()
    result = runner.invoke(app, [
        "init",
        "--name", "test",
        "--repo", "./new-repo:service",
        "--scaffold-taskfiles",
    ])
    assert result.exit_code == 0, result.output
    assert (no_taskfile / "Taskfile.yml").exists()


def test_init_no_args_no_tty(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    result = runner.invoke(app, ["init"])
    assert result.exit_code != 0

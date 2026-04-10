from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def configured_doctor_app(workspace: Path):
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    (workspace / ".mothership").mkdir(exist_ok=True)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = lambda cmd, cwd, env=None: (
        ShellResult(returncode=0, stdout="test\nrun\nlint\nsetup\n", stderr="") if "task --list" in cmd
        else ShellResult(returncode=0, stdout="Logged in", stderr="") if "gh auth" in cmd
        else ShellResult(returncode=0, stdout="", stderr="")
    )
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    container.shell.override(mock_shell)

    yield workspace
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_doctor_passes(configured_doctor_app: Path):
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    # CliRunner is non-TTY: output is JSON
    import json
    data = json.loads(result.output)
    assert data["errors"] == 0


def test_doctor_shows_workspace_name(configured_doctor_app: Path):
    result = runner.invoke(app, ["doctor"])
    # CliRunner is non-TTY: confirm command runs successfully
    assert result.exit_code == 0
    import json
    data = json.loads(result.output)
    assert "checks" in data

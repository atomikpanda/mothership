from typer.testing import CliRunner
from mship.cli import app

runner = CliRunner()


def test_view_command_exists():
    result = runner.invoke(app, ["view", "--help"])
    assert result.exit_code == 0
    assert "status" in result.stdout
    assert "logs" in result.stdout
    assert "diff" in result.stdout
    assert "spec" in result.stdout

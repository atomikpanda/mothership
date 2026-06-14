from typer.testing import CliRunner
from mship.cli import app

runner = CliRunner()


def test_serve_command_registered():
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "127.0.0.1" in result.output

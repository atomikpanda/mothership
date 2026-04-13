from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


def test_sync_clean_exits_zero(audit_workspace):
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(audit_workspace / ".mothership")
    try:
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert "up to date" in result.output or "up_to_date" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_sync_dirty_nonzero(audit_workspace):
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(audit_workspace / ".mothership")
    try:
        (audit_workspace / "cli" / "x.txt").write_text("x")
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1
        assert "dirty_worktree" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()

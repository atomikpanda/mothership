from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.workspace_meta import read_last_sync_at

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


def test_sync_records_last_workspace_fetch_at(audit_workspace):
    state_dir = audit_workspace / ".mothership"
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(state_dir)
    try:
        assert read_last_sync_at(state_dir) is None
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert read_last_sync_at(state_dir) is not None
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()

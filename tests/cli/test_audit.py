import json
from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


def _override(audit_workspace):
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(audit_workspace / ".mothership")


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()


def test_audit_clean_exits_zero(audit_workspace):
    _override(audit_workspace)
    try:
        result = runner.invoke(app, ["audit"])
        assert result.exit_code == 0, result.output
        assert "clean" in result.output
    finally:
        _reset()


def test_audit_dirty_exits_one(audit_workspace):
    _override(audit_workspace)
    try:
        (audit_workspace / "cli" / "x.txt").write_text("x")
        result = runner.invoke(app, ["audit"])
        assert result.exit_code == 1
        assert "dirty_worktree" in result.output
    finally:
        _reset()


def test_audit_json_shape(audit_workspace):
    _override(audit_workspace)
    try:
        (audit_workspace / "cli" / "x.txt").write_text("x")
        result = runner.invoke(app, ["audit", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["has_errors"] is True
        assert payload["workspace"] == "audit-test"
        cli_entry = next(r for r in payload["repos"] if r["name"] == "cli")
        codes = {i["code"] for i in cli_entry["issues"]}
        assert "dirty_worktree" in codes
    finally:
        _reset()


def test_audit_repos_filter_unknown(audit_workspace):
    _override(audit_workspace)
    try:
        result = runner.invoke(app, ["audit", "--repos", "cli,nope"])
        assert result.exit_code == 1
        assert "nope" in result.output
    finally:
        _reset()

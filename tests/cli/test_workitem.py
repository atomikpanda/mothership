import json

from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


def _isolate(tmp_path):
    """Point the global container at a throwaway workspace."""
    (tmp_path / "mothership.yaml").write_text("workspace: testws\nrepos: {}\n")
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    container.config.reset()  # drop any singleton cached by another test


def test_new_then_list_roundtrip(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Make capture conversational", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        res = runner.invoke(app, ["--json", "item", "list"])
        assert res.exit_code == 0, res.output
        rows = json.loads(res.output)
        assert len(rows) == 1
        assert rows[0]["title"] == "Make capture conversational"
        assert rows[0]["phase"] == "inbox"
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_new_with_invalid_kind_errors_cleanly(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "bogus"])
        assert res.exit_code == 1
        assert "invalid kind" in res.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_phase_with_invalid_value_errors_cleanly(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        item_id = res.output.strip()
        res = runner.invoke(app, ["item", "phase", item_id, "bogus"])
        assert res.exit_code == 1
        assert "invalid phase" in res.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()

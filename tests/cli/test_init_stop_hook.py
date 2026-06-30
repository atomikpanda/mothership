from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


def test_install_hooks_installs_stop_hook(tmp_path: Path):
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["init", "--install-hooks"])
        data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        assert "Stop" in data["hooks"]
        assert any(
            h.get("command") == "mship _drain"
            for e in data["hooks"]["Stop"]
            for h in e.get("hooks", [])
        )
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset_override(); container.config.reset()
        container.state_manager.reset_override(); container.state_manager.reset()
        container.log_manager.reset()

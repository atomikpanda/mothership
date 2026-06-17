import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _source_repo(root: Path) -> Path:
    src = root / "src"
    src.mkdir()
    _git(["init", "-q", "-b", "main"], src)
    (src / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    _git(["add", "."], src)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"], src)
    return src


def _ws(root: Path, body: str) -> Path:
    ws = root / "ws"
    ws.mkdir()
    (ws / "mothership.yaml").write_text(body)
    (ws / ".mothership").mkdir()
    return ws


def _configure(ws: Path):
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(ws / "mothership.yaml")
    container.state_dir.override(ws / ".mothership")


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_bootstrap_non_tty_is_pure_json(tmp_path):
    src = _source_repo(tmp_path)
    ws = _ws(tmp_path,
             "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
             f"    url: file://{src}\n")
    _configure(ws)
    try:
        result = runner.invoke(app, ["bootstrap"])
        data = json.loads(result.stdout)  # must be pure JSON
        assert any(m["name"] == "lib" and m["status"] == "cloned"
                   for m in data["members"])
        assert result.exit_code == 0
        assert (ws / "lib" / "Taskfile.yml").exists()
    finally:
        _reset()


def test_bootstrap_exit_nonzero_on_member_error(tmp_path):
    ws = _ws(tmp_path,
             "workspace: w\nrepos:\n  bad:\n    path: bad\n    type: library\n")
    _configure(ws)
    try:
        result = runner.invoke(app, ["bootstrap"])
        data = json.loads(result.stdout)
        assert data["members"][0]["status"] == "error"
        assert result.exit_code == 1
    finally:
        _reset()

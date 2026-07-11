"""CLI tests for `mship export` (MOS-102)."""
from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager

runner = CliRunner()

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _setup(ws: Path):
    container.config_path.override(ws / "mothership.yaml")
    container.state_dir.override(ws / ".mothership")


def _teardown():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def _commit(repo_dir: Path, filename: str, content: str, message: str):
    (repo_dir / filename).write_text(content)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True, env=_GIT_ENV)
    subprocess.run(["git", "commit", "-m", message], cwd=repo_dir, check=True, capture_output=True, env=_GIT_ENV)


def test_export_creates_dir_bundle_with_journal_and_state(workspace_with_git, monkeypatch):
    _setup(workspace_with_git)
    try:
        r = runner.invoke(app, ["spawn", "--hotfix", "add labels", "--repos", "shared", "--force-audit"])
        assert r.exit_code == 0, r.output
        runner.invoke(app, ["journal", "made progress", "--task", "add-labels"])

        monkeypatch.chdir(workspace_with_git)
        result = runner.invoke(app, ["export", "--task", "add-labels"])
        assert result.exit_code == 0, result.output

        bundle = workspace_with_git / "add-labels-export"
        assert bundle.is_dir()
        assert "made progress" in (bundle / "journal.md").read_text()
        state = json.loads((bundle / "state.json").read_text())
        assert state["slug"] == "add-labels"
        assert not (bundle / "spec.md").exists()
        assert not (bundle / "plan.md").exists()
    finally:
        _teardown()


def test_export_format_zip(workspace_with_git, monkeypatch):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "--hotfix", "zip export", "--repos", "shared", "--force-audit"])
        monkeypatch.chdir(workspace_with_git)
        result = runner.invoke(app, ["export", "--task", "zip-export", "--format", "zip"])
        assert result.exit_code == 0, result.output

        zip_path = workspace_with_git / "zip-export-export.zip"
        assert zip_path.is_file()
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            assert "zip-export-export/journal.md" in names
            assert "zip-export-export/state.json" in names
    finally:
        _teardown()


def test_export_invalid_format_errors(workspace_with_git, monkeypatch):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "--hotfix", "bad format", "--repos", "shared", "--force-audit"])
        monkeypatch.chdir(workspace_with_git)
        result = runner.invoke(app, ["export", "--task", "bad-format", "--format", "yaml"])
        assert result.exit_code != 0
        assert "format" in result.output.lower()
        assert not (workspace_with_git / "bad-format-export").exists()
    finally:
        _teardown()


def test_export_redacted_scrubs_planted_secret(workspace_with_git, monkeypatch):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "--hotfix", "redacted export", "--repos", "shared", "--force-audit"])
        runner.invoke(
            app,
            ["journal", "leaked API_KEY=abcdef123456 in a paste",
             "--task", "redacted-export"],
        )
        monkeypatch.chdir(workspace_with_git)

        result = runner.invoke(app, ["export", "--task", "redacted-export", "--redacted"])
        assert result.exit_code == 0, result.output
        journal = (workspace_with_git / "redacted-export-export" / "journal.md").read_text()
        assert "abcdef123456" not in journal
        assert "<REDACTED:env_secret>" in journal
    finally:
        _teardown()


def test_export_without_redacted_is_faithful(workspace_with_git, monkeypatch):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "--hotfix", "faithful export", "--repos", "shared", "--force-audit"])
        runner.invoke(
            app,
            ["journal", "leaked API_KEY=abcdef123456 in a paste",
             "--task", "faithful-export"],
        )
        monkeypatch.chdir(workspace_with_git)

        result = runner.invoke(app, ["export", "--task", "faithful-export"])
        assert result.exit_code == 0, result.output
        journal = (workspace_with_git / "faithful-export-export" / "journal.md").read_text()
        assert "API_KEY=abcdef123456" in journal
    finally:
        _teardown()


def test_export_includes_diff_for_repo_with_commits_omits_repo_without(workspace_with_git, monkeypatch):
    _setup(workspace_with_git)
    try:
        runner.invoke(
            app, ["spawn", "--hotfix", "multi repo export", "--repos", "shared,auth-service",
                  "--force-audit"],
        )
        state = StateManager(workspace_with_git / ".mothership").load()
        task = state.tasks["multi-repo-export"]
        shared_wt = Path(task.worktrees["shared"])
        _commit(shared_wt, "secret.env", "API_KEY=abcdef123456\n", "add secret file")

        monkeypatch.chdir(workspace_with_git)
        result = runner.invoke(app, ["export", "--task", "multi-repo-export"])
        assert result.exit_code == 0, result.output

        bundle = workspace_with_git / "multi-repo-export-export"
        assert (bundle / "diffs" / "shared.diff").is_file()
        assert "API_KEY=abcdef123456" in (bundle / "diffs" / "shared.diff").read_text()
        assert not (bundle / "diffs" / "auth-service.diff").exists()
    finally:
        _teardown()


def test_export_no_active_task_errors(workspace_with_git):
    _setup(workspace_with_git)
    try:
        result = runner.invoke(app, ["export"])
        assert result.exit_code != 0
    finally:
        _teardown()

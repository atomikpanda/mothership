from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.config import ConfigLoader
from mship.core.doctor import DoctorChecker, DoctorReport
from mship.util.shell import ShellRunner, ShellResult


def test_doctor_healthy_workspace(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    mock_shell = MagicMock(spec=ShellRunner)
    # task --list returns all standard tasks
    mock_shell.run.side_effect = lambda cmd, cwd, env=None: (
        ShellResult(returncode=0, stdout="test\nrun\nlint\nsetup\n", stderr="") if "task --list" in cmd
        else ShellResult(returncode=0, stdout="Logged in", stderr="") if "gh auth" in cmd
        else ShellResult(returncode=0, stdout="", stderr="")
    )

    checker = DoctorChecker(config, mock_shell)
    report = checker.run()
    assert report.ok
    assert report.errors == 0


def test_doctor_missing_git(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    # Remove .git from a repo (repos in workspace fixture don't have .git)
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="test\nrun\nlint\nsetup\n", stderr="")

    checker = DoctorChecker(config, mock_shell)
    report = checker.run()
    # No .git dirs → warnings
    git_checks = [c for c in report.checks if "git" in c.name]
    assert all(c.status == "warn" for c in git_checks)
    assert report.ok  # warnings don't cause failure


def test_doctor_gh_not_installed(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = lambda cmd, cwd, env=None: (
        ShellResult(returncode=0, stdout="test\nrun\nlint\nsetup\n", stderr="") if "task --list" in cmd
        else ShellResult(returncode=127, stdout="", stderr="not found") if "gh auth" in cmd
        else ShellResult(returncode=0, stdout="", stderr="")
    )

    checker = DoctorChecker(config, mock_shell)
    report = checker.run()
    gh_check = next(c for c in report.checks if c.name == "gh")
    assert gh_check.status == "warn"
    assert report.ok  # gh is optional


def test_doctor_resolves_task_aliases(tmp_path: Path):
    """Doctor should check the aliased task name, not the canonical name."""
    repo_dir = tmp_path / "my-app"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'")

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  my-app:
    path: ./my-app
    type: service
    tasks:
      run: dev
"""
    )
    config = ConfigLoader.load(cfg)

    mock_shell = MagicMock(spec=ShellRunner)
    # task --list output contains "dev" but not "run"
    mock_shell.run.side_effect = lambda cmd, cwd, env=None: (
        ShellResult(returncode=0, stdout="test\ndev\nlint\nsetup\n", stderr="")
        if "task --list" in cmd
        else ShellResult(returncode=0, stdout="Logged in", stderr="")
    )

    checker = DoctorChecker(config, mock_shell)
    report = checker.run()

    # The "run" check should pass because "dev" exists (it's the alias)
    run_check = next(c for c in report.checks if c.name == "my-app/task:run")
    assert run_check.status == "pass"
    assert "dev" in run_check.message


def test_doctor_warns_when_alias_missing(tmp_path: Path):
    repo_dir = tmp_path / "my-app"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'")

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  my-app:
    path: ./my-app
    type: service
    tasks:
      run: nonexistent
"""
    )
    config = ConfigLoader.load(cfg)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = lambda cmd, cwd, env=None: (
        ShellResult(returncode=0, stdout="test\nlint\nsetup\n", stderr="")
        if "task --list" in cmd
        else ShellResult(returncode=0, stdout="Logged in", stderr="")
    )

    checker = DoctorChecker(config, mock_shell)
    report = checker.run()

    run_check = next(c for c in report.checks if c.name == "my-app/task:run")
    assert run_check.status == "warn"
    assert "nonexistent" in run_check.message
    assert "aliased" in run_check.message


def test_doctor_report_properties():
    report = DoctorReport()
    from mship.core.doctor import CheckResult
    report.checks = [
        CheckResult(name="a", status="pass", message="ok"),
        CheckResult(name="b", status="warn", message="warning"),
        CheckResult(name="c", status="fail", message="error"),
    ]
    assert report.warnings == 1
    assert report.errors == 1
    assert report.ok is False

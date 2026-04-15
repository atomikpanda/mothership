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


def test_doctor_reports_taskfile_parse_error(tmp_path: Path):
    """When `task --list` returns non-zero, doctor emits a fail check."""
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
"""
    )
    config = ConfigLoader.load(cfg)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = lambda cmd, cwd, env=None: (
        ShellResult(returncode=1, stdout="", stderr="err: invalid keys in command\nfile: Taskfile.yml:7:9")
        if "task --list" in cmd
        else ShellResult(returncode=0, stdout="Logged in", stderr="")
    )

    checker = DoctorChecker(config, mock_shell)
    report = checker.run()

    parse_check = next(
        (c for c in report.checks if c.name == "my-app/taskfile_parse"),
        None,
    )
    assert parse_check is not None
    assert parse_check.status == "fail"
    assert "parse" in parse_check.message.lower() or "invalid keys" in parse_check.message

    # Per-task checks should NOT have been emitted
    task_checks = [c for c in report.checks if "my-app/task:" in c.name]
    assert task_checks == []


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


def test_doctor_resolves_git_root_subdir_paths(tmp_path: Path):
    """Doctor must use effective path for git_root subdir repos."""
    root = tmp_path / "monorepo"
    root.mkdir()
    (root / "Taskfile.yml").write_text("version: '3'")
    (root / ".git").mkdir()
    web = root / "web"
    web.mkdir()
    (web / "Taskfile.yml").write_text("version: '3'")

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: mono
repos:
  root:
    path: ./monorepo
    type: service
  web:
    path: web
    type: service
    git_root: root
"""
    )
    config = ConfigLoader.load(cfg)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="test\nrun\nlint\nsetup\n", stderr="")

    checker = DoctorChecker(config, mock_shell)
    report = checker.run()

    # web's path check should pass (resolved via git_root)
    web_path = next(c for c in report.checks if c.name == "web/path")
    assert web_path.status == "pass"
    web_taskfile = next(c for c in report.checks if c.name == "web/taskfile")
    assert web_taskfile.status == "pass"
    # web's git check should pass — git lives at parent's path
    web_git = next(c for c in report.checks if c.name == "web/git")
    assert web_git.status == "pass"


def test_doctor_warns_when_hook_missing(tmp_path):
    """Fresh workspace with a git repo but no hook installed → warn."""
    import subprocess
    from mship.core.config import ConfigLoader
    from mship.core.doctor import DoctorChecker
    from mship.util.shell import ShellRunner

    repo = tmp_path / "cli"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n  cli:\n    path: ./cli\n    type: service\n"
    )
    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    checker = DoctorChecker(cfg, ShellRunner())
    report = checker.run()
    hook_checks = [c for c in report.checks if "hook" in c.name.lower()]
    assert hook_checks, "expected a hook-related check"
    missing = [c for c in hook_checks if c.status == "warn"]
    assert missing
    assert any("install-hooks" in c.message or "pre-commit" in c.message.lower() for c in missing)


def test_doctor_passes_when_hook_installed(tmp_path):
    import subprocess
    from mship.core.config import ConfigLoader
    from mship.core.doctor import DoctorChecker
    from mship.core.hooks import install_hook
    from mship.util.shell import ShellRunner

    repo = tmp_path / "cli"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    install_hook(repo)

    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n  cli:\n    path: ./cli\n    type: service\n"
    )
    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    checker = DoctorChecker(cfg, ShellRunner())
    report = checker.run()
    hook_checks = [c for c in report.checks if "hook" in c.name.lower()]
    assert any(c.status == "pass" for c in hook_checks)
    # Must not have a warn-level hook check
    assert not any(c.status == "warn" for c in hook_checks)


def test_doctor_dedupes_hook_checks_in_monorepo(tmp_path):
    """Three repos sharing one git_root → one hook check, not three."""
    import subprocess
    import yaml
    from mship.core.config import ConfigLoader
    from mship.core.doctor import DoctorChecker
    from mship.util.shell import ShellRunner

    mono = tmp_path / "mono"
    mono.mkdir()
    (mono / "pkg-a").mkdir()
    (mono / "pkg-b").mkdir()
    subprocess.run(["git", "init", "-q", str(mono)], check=True, capture_output=True)
    for p in (mono, mono / "pkg-a", mono / "pkg-b"):
        (p / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")

    (tmp_path / "mothership.yaml").write_text(yaml.safe_dump({
        "workspace": "m",
        "repos": {
            "mono":  {"path": "./mono", "type": "service"},
            "pkg_a": {"path": "pkg-a", "type": "library", "git_root": "mono"},
            "pkg_b": {"path": "pkg-b", "type": "library", "git_root": "mono"},
        },
    }))
    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    checker = DoctorChecker(cfg, ShellRunner())
    report = checker.run()
    hook_checks = [c for c in report.checks if "hook" in c.name.lower()]
    assert len(hook_checks) == 1

"""Integration tests for `mship commit`. See #29."""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


def _set_finished(workspace: Path, slug: str, pr_urls: dict[str, str]) -> None:
    """Mark a spawned task as finished with given pr_urls."""
    import yaml
    from datetime import datetime, timezone
    state_path = workspace / ".mothership" / "state.yaml"
    data = yaml.safe_load(state_path.read_text())
    data["tasks"][slug]["finished_at"] = datetime.now(timezone.utc).isoformat()
    data["tasks"][slug]["pr_urls"] = pr_urls
    state_path.write_text(yaml.safe_dump(data))


def test_commit_pre_finish_single_repo(configured_git_app: Path):
    """Stage in one worktree pre-finish → commit + journal, no push."""
    runner.invoke(app, ["spawn", "pre finish commit", "--repos", "shared"])
    slug = "pre-finish-commit"

    commits: list[str] = []
    pushes: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            if "shared" in str(cwd):
                return ShellResult(returncode=1, stdout="", stderr="")
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git commit -m" in cmd:
            commits.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse HEAD" in cmd:
            return ShellResult(returncode=0, stdout="7f3a1b2abcdef\n", stderr="")
        if "git push" in cmd:
            pushes.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "fix: typo", "--task", slug])
        assert result.exit_code == 0, result.output
        assert len(commits) == 1 and "shared" in commits[0]
        assert pushes == []
        log = (configured_git_app / ".mothership" / "logs" / f"{slug}.md").read_text()
        assert "fix: typo" in log
        assert "repo=shared" in log
        assert "action=committed" in log
    finally:
        container.shell.reset_override()


def test_commit_pre_finish_multi_repo(configured_git_app: Path):
    """Stage in two worktrees → both commit, no push, two journal entries."""
    runner.invoke(app, ["spawn", "multi pre", "--repos", "shared,auth-service"])
    slug = "multi-pre"

    commits: list[str] = []
    pushes: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")
        if "git commit -m" in cmd:
            commits.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse HEAD" in cmd:
            return ShellResult(returncode=0, stdout="abc123def\n", stderr="")
        if "git push" in cmd:
            pushes.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "feat: coordinated", "--task", slug])
        assert result.exit_code == 0, result.output
        assert len(commits) == 2
        assert pushes == []
        log = (configured_git_app / ".mothership" / "logs" / f"{slug}.md").read_text()
        assert log.count("feat: coordinated") == 2
        assert "repo=shared" in log
        assert "repo=auth-service" in log
    finally:
        container.shell.reset_override()


def test_commit_post_finish_single_repo(configured_git_app: Path):
    """Finished task + PR → commit + push + journal."""
    runner.invoke(app, ["spawn", "post single", "--repos", "shared"])
    slug = "post-single"
    _set_finished(configured_git_app, slug, {"shared": "https://github.com/o/r/pull/7"})

    commits: list[str] = []
    pushes: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")
        if "git commit -m" in cmd:
            commits.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse HEAD" in cmd:
            return ShellResult(returncode=0, stdout="deadbeef\n", stderr="")
        if "git push" in cmd:
            pushes.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "fix: review", "--task", slug])
        assert result.exit_code == 0, result.output
        assert len(commits) == 1
        assert len(pushes) == 1 and "shared" in pushes[0]
    finally:
        container.shell.reset_override()


def test_commit_post_finish_multi_repo(configured_git_app: Path):
    """Finished multi-repo task → both commit + push."""
    runner.invoke(app, ["spawn", "post multi", "--repos", "shared,auth-service"])
    slug = "post-multi"
    _set_finished(configured_git_app, slug, {
        "shared": "https://github.com/o/r/pull/1",
        "auth-service": "https://github.com/o/r/pull/2",
    })

    commits: list[str] = []
    pushes: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")
        if "git commit -m" in cmd:
            commits.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse HEAD" in cmd:
            return ShellResult(returncode=0, stdout="abcdef12\n", stderr="")
        if "git push" in cmd:
            pushes.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "fix: both repos", "--task", slug])
        assert result.exit_code == 0, result.output
        assert len(commits) == 2
        assert len(pushes) == 2
    finally:
        container.shell.reset_override()


def test_commit_skips_repos_without_staged_changes(configured_git_app: Path):
    """Partial staging: one repo staged, one not → only first commits."""
    runner.invoke(app, ["spawn", "partial", "--repos", "shared,auth-service"])
    slug = "partial"

    commits: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            if "shared" in str(cwd):
                return ShellResult(returncode=1, stdout="", stderr="")
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git commit -m" in cmd:
            commits.append(str(cwd))
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse HEAD" in cmd:
            return ShellResult(returncode=0, stdout="cafe1234\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "fix: just shared", "--task", slug])
        assert result.exit_code == 0, result.output
        assert len(commits) == 1 and "shared" in commits[0]
        assert "auth-service" in result.output
        assert "skipped" in result.output.lower() or "nothing staged" in result.output.lower()
    finally:
        container.shell.reset_override()


def test_commit_errors_when_nothing_staged_anywhere(configured_git_app: Path):
    """No staged changes in any worktree → exit 1 with clear message."""
    runner.invoke(app, ["spawn", "nothing", "--repos", "shared,auth-service"])
    slug = "nothing"

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "nope", "--task", slug])
        assert result.exit_code != 0
        assert "nothing staged" in result.output.lower()
        assert "git add" in result.output.lower()
    finally:
        container.shell.reset_override()


def test_commit_git_commit_failure_surfaces(configured_git_app: Path):
    """Hook rejection during git commit → exit 1, error message surfaces stderr."""
    runner.invoke(app, ["spawn", "hook fail", "--repos", "shared"])
    slug = "hook-fail"

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")
        if "git commit -m" in cmd:
            return ShellResult(
                returncode=1, stdout="",
                stderr="pre-commit hook failed: lint errors",
            )
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "will fail", "--task", slug])
        assert result.exit_code != 0
        assert "shared" in result.output
        assert "pre-commit hook failed" in result.output
    finally:
        container.shell.reset_override()


def test_commit_push_failure_surfaces_post_finish(configured_git_app: Path):
    """Push fails post-finish → exit 1, but journal DOES record the commit."""
    runner.invoke(app, ["spawn", "push fail", "--repos", "shared"])
    slug = "push-fail"
    _set_finished(configured_git_app, slug, {"shared": "https://github.com/o/r/pull/1"})

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")
        if "git commit -m" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse HEAD" in cmd:
            return ShellResult(returncode=0, stdout="1a2b3c4\n", stderr="")
        if "git push" in cmd:
            return ShellResult(
                returncode=1, stdout="",
                stderr="! [rejected] feat/branch (non-fast-forward)",
            )
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "fix: will-fail-push", "--task", slug])
        assert result.exit_code != 0
        assert "push" in result.output.lower()
        assert "shared" in result.output
        log = (configured_git_app / ".mothership" / "logs" / f"{slug}.md").read_text()
        assert "fix: will-fail-push" in log
    finally:
        container.shell.reset_override()

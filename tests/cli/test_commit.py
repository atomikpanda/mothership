"""Integration tests for `mship commit`. See #29."""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.cli.output import reset_output_settings
from mship.core.state import StateManager
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


def _set_finished(workspace: Path, slug: str, pr_urls: dict[str, str]) -> None:
    """Mark a spawned task as finished with given pr_urls."""
    from datetime import datetime, timezone
    manager = StateManager(workspace / ".mothership")

    def _update(state):
        state.tasks[slug].finished_at = datetime.now(timezone.utc)
        state.tasks[slug].pr_urls = pr_urls

    manager.mutate(_update)


def test_commit_pre_finish_single_repo(configured_git_app: Path):
    """Stage in one worktree pre-finish → commit + journal, no push."""
    runner.invoke(app, ["spawn", "--hotfix", "pre finish commit", "--repos", "shared"])
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


def test_commit_journals_commit_sha_as_evidence(configured_git_app: Path):
    """Committed sha is journaled into LogEntry.evidence (commit:<sha> ref)."""
    runner.invoke(app, ["spawn", "--hotfix", "evidence commit", "--repos", "shared"])
    slug = "evidence-commit"

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")
        if "git commit -m" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-parse HEAD" in cmd:
            return ShellResult(returncode=0, stdout="7f3a1b2abcdef\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "fix: typo", "--task", slug])
        assert result.exit_code == 0, result.output
        log = (configured_git_app / ".mothership" / "logs" / f"{slug}.md").read_text()
        assert 'evidence="7f3a1b2abcdef"' in log
        assert "action=committed" in log
    finally:
        container.shell.reset_override()


def test_commit_pre_finish_multi_repo(configured_git_app: Path):
    """Stage in two worktrees → both commit, no push, two journal entries."""
    runner.invoke(app, ["spawn", "--hotfix", "multi pre", "--repos", "shared,auth-service"])
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
    runner.invoke(app, ["spawn", "--hotfix", "post single", "--repos", "shared"])
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
    runner.invoke(app, ["spawn", "--hotfix", "post multi", "--repos", "shared,auth-service"])
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


def _git_at(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _finished_with_remotes(workspace: Path, slug: str, repos: list[str]):
    result = runner.invoke(
        app, ["spawn", "--hotfix", slug, "--repos", ",".join(repos)],
    )
    assert result.exit_code == 0, result.output
    task = StateManager(workspace / ".mothership").load().tasks[slug]
    worktrees = {repo: Path(task.worktrees[repo]) for repo in repos}
    remotes = {}
    for repo, worktree in worktrees.items():
        remote = workspace / f"{repo}-origin.git"
        _git_at(workspace, "init", "--bare", str(remote))
        _git_at(worktree, "remote", "add", "origin", str(remote))
        _git_at(worktree, "push", "-u", "origin", task.branch)
        remotes[repo] = remote
    _set_finished(workspace, slug, {
        repo: f"https://example.invalid/{repo}/pull/1" for repo in repos
    })
    return worktrees, remotes, task.branch


def test_commit_pushes_existing_and_staged_work_then_noops(configured_git_app: Path):
    """Mixed post-finish work reaches both remotes without duplicating commits."""
    workspace = configured_git_app
    slug = "mixed-existing"
    worktrees, remotes, branch = _finished_with_remotes(
        workspace, slug, ["shared", "auth-service"],
    )
    for worktree in worktrees.values():
        (worktree / "fix.txt").write_text("review fix\n")
        _git_at(worktree, "add", "fix.txt")
    _git_at(worktrees["shared"], "commit", "-m", "already committed")
    existing_sha = _git_at(worktrees["shared"], "rev-parse", "HEAD")
    staged_base = _git_at(worktrees["auth-service"], "rev-parse", "HEAD")
    container.shell.override(ShellRunner())
    try:
        result = runner.invoke(app, ["--json", "commit", "finish fixes", "--task", slug])
        assert result.exit_code == 0, result.output
        actions = {repo["repo"]: repo["action"] for repo in json.loads(result.output)["repos"]}
        assert actions == {"shared": "pushed", "auth-service": "committed"}
        assert _git_at(worktrees["shared"], "rev-parse", "HEAD") == existing_sha
        assert _git_at(worktrees["auth-service"], "rev-parse", "HEAD^") == staged_base
        for repo, worktree in worktrees.items():
            assert _git_at(remotes[repo], "rev-parse", branch) == _git_at(worktree, "rev-parse", "HEAD")
            assert _git_at(worktree, "show", "HEAD:fix.txt") == "review fix"
        manager = StateManager(workspace / ".mothership")
        activity = manager.load().tasks[slug].last_activity_at
        retry = runner.invoke(app, ["--json", "commit", "already delivered", "--task", slug])
        assert retry.exit_code == 0, retry.output
        assert all("skipped" in repo for repo in json.loads(retry.output)["repos"])
        assert manager.load().tasks[slug].last_activity_at == activity
        assert _git_at(worktrees["shared"], "rev-parse", "HEAD") == existing_sha
        log = (workspace / ".mothership" / "logs" / f"{slug}.md").read_text()
        assert "action=pushed" in log and "action=committed" in log
    finally:
        reset_output_settings()
        container.shell.reset_override()


def test_commit_push_only_errors_when_upstream_is_missing(configured_git_app: Path):
    """Missing upstream cannot be mistaken for a synchronized finished branch."""
    worktrees, remotes, branch = _finished_with_remotes(
        configured_git_app, "missing-upstream", ["shared"],
    )
    worktree = worktrees["shared"]
    before = _git_at(worktree, "rev-parse", "HEAD")
    _git_at(worktree, "branch", "--unset-upstream")
    container.shell.override(ShellRunner())
    try:
        result = runner.invoke(app, ["commit", "deliver", "--task", "missing-upstream"])
        assert result.exit_code == 1
        assert "upstream" in result.output.lower()
        assert _git_at(worktree, "rev-parse", "HEAD") == before
        assert _git_at(remotes["shared"], "rev-parse", branch) == before
    finally:
        container.shell.reset_override()


def test_commit_push_only_failure_is_retriable(configured_git_app: Path):
    """A real remote rejection preserves the local commit for a successful retry."""
    workspace = configured_git_app
    slug = "retry-push"
    worktrees, remotes, branch = _finished_with_remotes(workspace, slug, ["shared"])
    worktree = worktrees["shared"]
    remote = remotes["shared"]
    remote_before = _git_at(remote, "rev-parse", branch)
    (worktree / "fix.txt").write_text("already committed\n")
    _git_at(worktree, "add", "fix.txt")
    _git_at(worktree, "commit", "-m", "already made")
    local_sha = _git_at(worktree, "rev-parse", "HEAD")
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    container.shell.override(ShellRunner())
    try:
        failed = runner.invoke(app, ["commit", "deliver", "--task", slug])
        assert failed.exit_code == 1
        assert _git_at(remote, "rev-parse", branch) == remote_before
        log_path = workspace / ".mothership" / "logs" / f"{slug}.md"
        assert "action=pushed" not in log_path.read_text()
        hook.unlink()
        retried = runner.invoke(app, ["commit", "deliver", "--task", slug])
        assert retried.exit_code == 0, retried.output
        assert _git_at(remote, "rev-parse", branch) == local_sha
        assert _git_at(worktree, "rev-parse", "HEAD") == local_sha
        assert "action=pushed" in log_path.read_text()
        assert "action=committed" not in log_path.read_text()
    finally:
        container.shell.reset_override()


def test_commit_skips_repos_without_staged_changes(configured_git_app: Path):
    """Partial staging: one repo staged, one not → only first commits."""
    runner.invoke(app, ["spawn", "--hotfix", "partial", "--repos", "shared,auth-service"])
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
    runner.invoke(app, ["spawn", "--hotfix", "nothing", "--repos", "shared,auth-service"])
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
    runner.invoke(app, ["spawn", "--hotfix", "hook fail", "--repos", "shared"])
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
    runner.invoke(app, ["spawn", "--hotfix", "push fail", "--repos", "shared"])
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


def test_commit_stamps_last_activity(configured_git_app: Path):
    """A successful commit stamps the task's activity heartbeat."""
    runner.invoke(app, ["spawn", "--hotfix", "activity commit", "--repos", "shared"])
    slug = "activity-commit"

    def mock_run(cmd, cwd, env=None):
        if "git diff --cached --quiet" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="")  # staged
        if "git rev-parse HEAD" in cmd:
            return ShellResult(returncode=0, stdout="7f3a1b2abcdef\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["commit", "fix: thing", "--task", slug])
        assert result.exit_code == 0, result.output
        state = StateManager(configured_git_app / ".mothership").load()
        assert state.tasks[slug].last_activity_at is not None
    finally:
        container.shell.reset_override()

"""Integration tests for `mship pr`. See #41."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from mship.cli import app, container
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


def _set_pr_urls(workspace: Path, slug: str, pr_urls: dict[str, str]) -> None:
    from datetime import datetime, timezone
    state_path = workspace / ".mothership" / "state.yaml"
    data = yaml.safe_load(state_path.read_text())
    data["tasks"][slug]["pr_urls"] = pr_urls
    data["tasks"][slug]["finished_at"] = datetime.now(timezone.utc).isoformat()
    state_path.write_text(yaml.safe_dump(data))


def _pr_view_shell(mapping: dict[str, tuple[str, int, str]]):
    """Build a mock shell side_effect.

    `mapping` is {url: (state, number, base)}; returns unknown for misses.
    """
    def _run(cmd, cwd, env=None):
        if "gh pr view" in cmd:
            for url, (state, number, base) in mapping.items():
                if url in cmd:
                    return ShellResult(
                        returncode=0,
                        stdout=f"{state}\t{number}\t{base}\n",
                        stderr="",
                    )
            return ShellResult(returncode=1, stdout="", stderr="unknown")
        # Fall through to audit defaults.
        from tests.cli.conftest import _audit_ok_run
        return _audit_ok_run(cmd, cwd, env)
    return _run


def test_pr_empty_when_no_tasks(configured_git_app: Path):
    result = runner.invoke(app, ["pr"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {"tasks": []}


def test_pr_lists_tasks_with_pr_urls(configured_git_app: Path):
    runner.invoke(app, ["spawn", "first", "--repos", "shared", "--skip-setup"])
    runner.invoke(app, ["spawn", "second", "--repos", "shared", "--skip-setup"])
    _set_pr_urls(configured_git_app, "first", {"shared": "https://github.com/o/r/pull/1"})
    _set_pr_urls(configured_git_app, "second", {"shared": "https://github.com/o/r/pull/2"})

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = _pr_view_shell({
        "pull/1": ("OPEN", 1, "main"),
        "pull/2": ("MERGED", 2, "main"),
    })
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["pr"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        slugs = [t["slug"] for t in payload["tasks"]]
        assert "first" in slugs and "second" in slugs
        states = {t["slug"]: t["prs"][0]["state"] for t in payload["tasks"]}
        assert states["first"] == "open"
        assert states["second"] == "merged"
    finally:
        container.shell.reset_override()


def test_pr_skips_tasks_without_pr_urls(configured_git_app: Path):
    runner.invoke(app, ["spawn", "no pr", "--repos", "shared", "--skip-setup"])
    # Don't set pr_urls.
    result = runner.invoke(app, ["pr"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {"tasks": []}


def test_pr_gh_failure_shows_unknown(configured_git_app: Path):
    runner.invoke(app, ["spawn", "fail", "--repos", "shared", "--skip-setup"])
    _set_pr_urls(configured_git_app, "fail", {"shared": "https://github.com/o/r/pull/9"})

    mock_shell = MagicMock(spec=ShellRunner)

    def _run(cmd, cwd, env=None):
        if "gh pr view" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="rate limit exceeded")
        from tests.cli.conftest import _audit_ok_run
        return _audit_ok_run(cmd, cwd, env)

    mock_shell.run.side_effect = _run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["pr"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["tasks"][0]["prs"][0]["state"] == "unknown"
    finally:
        container.shell.reset_override()


def test_pr_multiple_repos_per_task(configured_git_app: Path):
    runner.invoke(app, ["spawn", "multi", "--repos", "shared,auth-service", "--skip-setup"])
    _set_pr_urls(configured_git_app, "multi", {
        "shared": "https://github.com/o/r/pull/1",
        "auth-service": "https://github.com/o/r/pull/2",
    })

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = _pr_view_shell({
        "pull/1": ("OPEN", 1, "main"),
        "pull/2": ("OPEN", 2, "main"),
    })
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["pr"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert len(payload["tasks"]) == 1
        assert len(payload["tasks"][0]["prs"]) == 2
        repos = {p["repo"] for p in payload["tasks"][0]["prs"]}
        assert repos == {"shared", "auth-service"}
    finally:
        container.shell.reset_override()

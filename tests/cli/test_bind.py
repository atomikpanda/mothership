"""Integration tests for `mship bind refresh`. See #71.

Core refresh behavior is tested directly on `WorktreeManager.refresh_bind_files`
in `tests/core/test_worktree.py`. These tests verify CLI wiring: JSON output,
exit code on conflict, --overwrite flag, --repos filter, unknown-repo rejection.
"""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


@pytest.fixture
def patch_refresh(monkeypatch):
    """Patch WorktreeManager.refresh_bind_files at the class level so every
    Factory-created instance sees the mock."""
    calls: list[dict] = []
    default_result = {
        "copied": [], "updated": [], "unchanged": [],
        "skipped": [], "warnings": [],
    }
    result_by_repo: dict[str, dict] = {}

    def _mock(self, repo_name, repo_config, worktree_path, overwrite=False):
        calls.append({"repo": repo_name, "overwrite": overwrite})
        return result_by_repo.get(repo_name, default_result)

    from mship.core.worktree import WorktreeManager
    monkeypatch.setattr(WorktreeManager, "refresh_bind_files", _mock)

    def _set(repo_name: str, **kwargs):
        result_by_repo[repo_name] = {**default_result, **kwargs}

    _set.calls = calls  # type: ignore[attr-defined]
    return _set


def test_bind_refresh_reports_copied(configured_git_app: Path, patch_refresh):
    runner.invoke(app, ["spawn", "copied test", "--repos", "shared", "--skip-setup"])
    patch_refresh("shared", copied=[".env"])
    result = runner.invoke(app, ["bind", "refresh", "--task", "copied-test"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["task"] == "copied-test"
    assert payload["repos"][0]["copied"] == [".env"]


def test_bind_refresh_exits_nonzero_on_skipped_without_overwrite(configured_git_app: Path, patch_refresh):
    runner.invoke(app, ["spawn", "skip test", "--repos", "shared", "--skip-setup"])
    patch_refresh("shared", skipped=[".env"])
    result = runner.invoke(app, ["bind", "refresh", "--task", "skip-test"])
    assert result.exit_code != 0


def test_bind_refresh_overwrite_flag_passed_through(configured_git_app: Path, patch_refresh):
    runner.invoke(app, ["spawn", "ow test", "--repos", "shared", "--skip-setup"])
    patch_refresh("shared", updated=[".env"])
    result = runner.invoke(app, ["bind", "refresh", "--task", "ow-test", "--overwrite"])
    assert result.exit_code == 0, result.output
    # Confirm overwrite=True flowed through to the mocked method.
    assert patch_refresh.calls[0]["overwrite"] is True


def test_bind_refresh_unknown_repo_errors(configured_git_app: Path, patch_refresh):
    runner.invoke(app, ["spawn", "unk", "--repos", "shared", "--skip-setup"])
    result = runner.invoke(
        app, ["bind", "refresh", "--task", "unk", "--repos", "not-a-repo"],
    )
    assert result.exit_code != 0
    assert "not-a-repo" in result.output


def test_bind_refresh_repos_filter_scopes(configured_git_app: Path, patch_refresh):
    runner.invoke(
        app, ["spawn", "multi", "--repos", "shared,auth-service", "--skip-setup"],
    )
    result = runner.invoke(
        app, ["bind", "refresh", "--task", "multi", "--repos", "shared"],
    )
    assert result.exit_code == 0, result.output
    repos_called = [c["repo"] for c in patch_refresh.calls]
    assert repos_called == ["shared"]  # auth-service NOT called


def test_bind_refresh_surfaces_warnings(configured_git_app: Path, patch_refresh):
    runner.invoke(app, ["spawn", "warn", "--repos", "shared", "--skip-setup"])
    patch_refresh("shared", warnings=["shared: bind_files source missing: .absent"])
    result = runner.invoke(app, ["bind", "refresh", "--task", "warn"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert any(".absent" in w for w in payload["repos"][0]["warnings"])

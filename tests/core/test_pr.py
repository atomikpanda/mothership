from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.pr import PRManager
from mship.util.shell import ShellRunner, ShellResult


@pytest.fixture
def mock_shell() -> MagicMock:
    shell = MagicMock(spec=ShellRunner)
    return shell


def test_check_gh_available_success(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="Logged in", stderr="")
    mgr = PRManager(mock_shell)
    mgr.check_gh_available()


def test_check_gh_available_not_installed(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=127, stdout="", stderr="command not found")
    mgr = PRManager(mock_shell)
    with pytest.raises(RuntimeError, match="gh"):
        mgr.check_gh_available()


def test_check_gh_available_not_authenticated(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=1, stdout="", stderr="not logged in")
    mgr = PRManager(mock_shell)
    with pytest.raises(RuntimeError, match="auth"):
        mgr.check_gh_available()


def test_push_branch(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mgr = PRManager(mock_shell)
    mgr.push_branch(Path("/tmp/repo"), "feat/test")
    mock_shell.run.assert_called_once()
    cmd = mock_shell.run.call_args.args[0]
    assert "git push" in cmd
    assert "feat/test" in cmd


def test_create_pr(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(
        returncode=0,
        stdout="https://github.com/org/repo/pull/42\n",
        stderr="",
    )
    mgr = PRManager(mock_shell)
    url = mgr.create_pr(
        repo_path=Path("/tmp/repo"),
        branch="feat/test",
        title="Add labels",
        body="Task description",
    )
    assert url == "https://github.com/org/repo/pull/42"
    cmd = mock_shell.run.call_args.args[0]
    assert "gh pr create" in cmd


def test_create_pr_failure(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=1, stdout="", stderr="error")
    mgr = PRManager(mock_shell)
    with pytest.raises(RuntimeError, match="Failed to create PR"):
        mgr.create_pr(Path("/tmp/repo"), "feat/test", "title", "body")


def test_count_commits_ahead_parses_integer(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="3\n", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.count_commits_ahead(Path("/tmp/r"), "main", "feat/x") == 3
    cmd = mock_shell.run.call_args.args[0]
    assert "git rev-list --count" in cmd
    assert "origin/main..feat/x" in cmd


def test_count_commits_ahead_zero_for_empty_output(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="0\n", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.count_commits_ahead(Path("/tmp/r"), "main", "feat/x") == 0


def test_count_commits_ahead_zero_on_git_failure(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=128, stdout="", stderr="bad ref")
    mgr = PRManager(mock_shell)
    assert mgr.count_commits_ahead(Path("/tmp/r"), "main", "feat/x") == 0


def test_update_pr_body(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mgr = PRManager(mock_shell)
    mgr.update_pr_body("https://github.com/org/repo/pull/42", "new body")
    cmd = mock_shell.run.call_args.args[0]
    assert "gh pr edit" in cmd


def test_get_pr_body(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="existing body\n", stderr="")
    mgr = PRManager(mock_shell)
    body = mgr.get_pr_body("https://github.com/org/repo/pull/42")
    assert body == "existing body"


def test_build_coordination_block():
    mgr = PRManager(MagicMock())
    prs = [
        {"repo": "shared", "url": "https://github.com/org/shared/pull/18", "order": 1},
        {"repo": "auth-service", "url": "https://github.com/org/auth/pull/42", "order": 2},
    ]
    block = mgr.build_coordination_block("add-labels", prs, current_repo="auth-service")
    assert "add-labels" in block
    assert "shared" in block
    assert "auth-service" in block
    assert "merge first" in block
    assert "this PR" in block


def test_build_coordination_block_single_repo():
    mgr = PRManager(MagicMock())
    prs = [
        {"repo": "shared", "url": "https://github.com/org/shared/pull/18", "order": 1},
    ]
    block = mgr.build_coordination_block("add-labels", prs, current_repo="shared")
    assert block == ""


def test_create_pr_with_base(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(
        returncode=0,
        stdout="https://github.com/org/repo/pull/42\n",
        stderr="",
    )
    mgr = PRManager(mock_shell)
    mgr.create_pr(
        repo_path=Path("/tmp/repo"),
        branch="feat/test",
        title="t",
        body="b",
        base="release/7",
    )
    cmd = mock_shell.run.call_args.args[0]
    assert "--base 'release/7'" in cmd or "--base release/7" in cmd


def test_create_pr_without_base_omits_flag(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(
        returncode=0,
        stdout="https://github.com/org/repo/pull/42\n",
        stderr="",
    )
    mgr = PRManager(mock_shell)
    mgr.create_pr(
        repo_path=Path("/tmp/repo"),
        branch="feat/test",
        title="t",
        body="b",
    )
    cmd = mock_shell.run.call_args.args[0]
    assert "--base" not in cmd


def test_verify_base_exists_true(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(
        returncode=0,
        stdout="abc123\trefs/heads/main\n",
        stderr="",
    )
    mgr = PRManager(mock_shell)
    assert mgr.verify_base_exists(Path("/tmp/repo"), "main") is True
    cmd = mock_shell.run.call_args.args[0]
    assert "git ls-remote --heads origin" in cmd
    assert "main" in cmd


def test_verify_base_exists_empty_output_false(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.verify_base_exists(Path("/tmp/repo"), "nope") is False


def test_verify_base_exists_nonzero_exit_false(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=128, stdout="", stderr="network err")
    mgr = PRManager(mock_shell)
    assert mgr.verify_base_exists(Path("/tmp/repo"), "main") is False


def test_check_pr_state_merged(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="MERGED\n", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_pr_state("https://github.com/o/r/pull/1") == "merged"
    cmd = mock_shell.run.call_args.args[0]
    assert "gh pr view" in cmd
    assert "--json state" in cmd


def test_check_pr_state_closed(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="CLOSED\n", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_pr_state("https://x/1") == "closed"


def test_check_pr_state_open(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="OPEN\n", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_pr_state("https://x/1") == "open"


def test_check_pr_state_unknown_on_failure(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=1, stdout="", stderr="not found")
    mgr = PRManager(mock_shell)
    assert mgr.check_pr_state("https://x/1") == "unknown"


def test_check_pr_state_unknown_on_unexpected_output(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="DRAFT\n", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_pr_state("https://x/1") == "unknown"


def test_check_merged_into_base_true(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_merged_into_base(Path("/tmp/repo"), "feat/x", "main") is True
    cmd = mock_shell.run.call_args.args[0]
    assert "git merge-base --is-ancestor" in cmd
    assert "feat/x" in cmd
    assert "main" in cmd


def test_check_merged_into_base_false_on_nonzero(mock_shell: MagicMock):
    # git merge-base --is-ancestor returns 1 when NOT an ancestor
    mock_shell.run.return_value = ShellResult(returncode=1, stdout="", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_merged_into_base(Path("/tmp/repo"), "feat/x", "main") is False


def test_check_pushed_to_origin_true_when_sha_matches(mock_shell: MagicMock):
    def side_effect(cmd, cwd, env=None):
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/feat/x\n", stderr="")
        if "git rev-parse" in cmd:
            return ShellResult(returncode=0, stdout="abc123\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")
    mock_shell.run.side_effect = side_effect
    mgr = PRManager(mock_shell)
    assert mgr.check_pushed_to_origin(Path("/tmp/repo"), "feat/x") is True


def test_check_pushed_to_origin_false_when_sha_differs(mock_shell: MagicMock):
    def side_effect(cmd, cwd, env=None):
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/feat/x\n", stderr="")
        if "git rev-parse" in cmd:
            return ShellResult(returncode=0, stdout="def456\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")
    mock_shell.run.side_effect = side_effect
    mgr = PRManager(mock_shell)
    assert mgr.check_pushed_to_origin(Path("/tmp/repo"), "feat/x") is False


def test_check_pushed_to_origin_false_when_branch_not_on_origin(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_pushed_to_origin(Path("/tmp/repo"), "feat/x") is False


def test_check_pushed_to_origin_false_on_ls_remote_failure(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=128, stdout="", stderr="network err")
    mgr = PRManager(mock_shell)
    assert mgr.check_pushed_to_origin(Path("/tmp/repo"), "feat/x") is False


# --- ensure_upstream (spec 2026-04-19) ---


def test_ensure_upstream_noop_when_already_set(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    # `git rev-parse --abbrev-ref --symbolic-full-name @{u}` returns 0 → already set.
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="origin/feat/x\n", stderr="")
    pr_mgr = PRManager(mock_shell)
    pr_mgr.ensure_upstream(Path("/repo"), "feat/x")
    assert mock_shell.run.call_count == 1
    called_cmd = mock_shell.run.call_args_list[0].args[0]
    assert "rev-parse" in called_cmd
    assert "@{u}" in called_cmd


def test_ensure_upstream_sets_tracking_when_missing(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    rc_results = [
        ShellResult(returncode=1, stdout="", stderr="fatal: no upstream"),  # rev-parse fails
        ShellResult(returncode=0, stdout="", stderr=""),                     # set-upstream-to succeeds
    ]
    mock_shell.run.side_effect = rc_results
    pr_mgr = PRManager(mock_shell)
    pr_mgr.ensure_upstream(Path("/repo"), "feat/x")
    assert mock_shell.run.call_count == 2
    second_cmd = mock_shell.run.call_args_list[1].args[0]
    assert "--set-upstream-to=origin/feat/x" in second_cmd
    assert "feat/x" in second_cmd


# --- list_pr_for_branch ---


def test_list_pr_for_branch_returns_url_when_present(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    mock_shell.run.return_value = ShellResult(
        returncode=0,
        stdout="https://github.com/org/repo/pull/17\n",
        stderr="",
    )
    pr_mgr = PRManager(mock_shell)
    url = pr_mgr.list_pr_for_branch(Path("/repo"), "feat/x")
    assert url == "https://github.com/org/repo/pull/17"
    cmd = mock_shell.run.call_args_list[0].args[0]
    assert "gh pr list" in cmd
    assert "--head" in cmd
    assert "--state all" in cmd
    assert "feat/x" in cmd


def test_list_pr_for_branch_returns_none_when_empty(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="\n", stderr="")
    pr_mgr = PRManager(mock_shell)
    assert pr_mgr.list_pr_for_branch(Path("/repo"), "feat/x") is None


def test_list_pr_for_branch_returns_none_on_gh_failure(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    mock_shell.run.return_value = ShellResult(returncode=1, stdout="", stderr="error")
    pr_mgr = PRManager(mock_shell)
    assert pr_mgr.list_pr_for_branch(Path("/repo"), "feat/x") is None


# --- create_pr duplicate-PR fallback ---


def test_create_pr_duplicate_harvests_existing_url(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    mock_shell.run.side_effect = [
        ShellResult(
            returncode=1,
            stdout="",
            stderr="a pull request for branch \"feat/x\" into branch \"main\" already exists",
        ),
        ShellResult(
            returncode=0,
            stdout="https://github.com/org/repo/pull/17\n",
            stderr="",
        ),
    ]
    pr_mgr = PRManager(mock_shell)
    url = pr_mgr.create_pr(
        repo_path=Path("/repo"), branch="feat/x",
        title="t", body="b", base="main",
    )
    assert url == "https://github.com/org/repo/pull/17"
    assert "gh pr create" in mock_shell.run.call_args_list[0].args[0]
    assert "gh pr list" in mock_shell.run.call_args_list[1].args[0]


def test_create_pr_duplicate_but_list_fails_raises(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    mock_shell.run.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="a pull request already exists"),
        ShellResult(returncode=1, stdout="", stderr="gh auth error"),
    ]
    pr_mgr = PRManager(mock_shell)
    with pytest.raises(RuntimeError, match="Failed to create PR"):
        pr_mgr.create_pr(
            repo_path=Path("/repo"), branch="feat/x",
            title="t", body="b", base="main",
        )


def test_create_pr_non_duplicate_error_still_raises(mock_shell: MagicMock):
    """Regression: non-duplicate rc=1 errors still raise (existing behavior)."""
    from mship.core.pr import PRManager
    mock_shell.run.return_value = ShellResult(
        returncode=1, stdout="", stderr="fatal: some other error",
    )
    pr_mgr = PRManager(mock_shell)
    with pytest.raises(RuntimeError, match="some other error"):
        pr_mgr.create_pr(
            repo_path=Path("/repo"), branch="feat/x",
            title="t", body="b", base="main",
        )

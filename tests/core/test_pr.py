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
    mock_shell.run.side_effect = [
        ShellResult(returncode=0, stdout="", stderr=""),   # gh auth status → gh_usable=True
        ShellResult(returncode=1, stdout="", stderr="error"),  # gh pr create fails
    ]
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


def _spec_with_acs(acs):
    from datetime import datetime, timezone
    from mship.core.spec import Spec
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    return Spec(id="dq", title="DQ", status="approved", created_at=now, updated_at=now,
                acceptance_criteria=acs)


def test_build_acceptance_block_renders_verified_and_unverified():
    from mship.core.pr import build_acceptance_block
    from mship.core.spec import AcceptanceCriterion, AcceptanceEvidence
    spec = _spec_with_acs([
        AcceptanceCriterion(id="ac1", text="does X", verdict="approved",
                            evidence=[AcceptanceEvidence(kind="test", ref="test-runs/5")]),
        AcceptanceCriterion(id="ac2", text="does Y"),   # no evidence
    ])
    block = build_acceptance_block(spec)
    assert "## Acceptance criteria" in block
    assert "ac1" in block and "test:test-runs/5" in block   # verified with its ref
    assert "ac2" in block and "no evidence" in block.lower()  # unverified


def test_build_acceptance_block_empty_when_no_criteria():
    from mship.core.pr import build_acceptance_block
    assert build_acceptance_block(_spec_with_acs([])) == ""


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
    assert mgr.check_pr_state("https://github.com/o/r/pull/1").state == "merged"
    cmd = mock_shell.run.call_args.args[0]
    assert "gh pr view" in cmd
    assert "--json state" in cmd


def test_check_pr_state_closed(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="CLOSED\n", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_pr_state("https://x/1").state == "closed"


def test_check_pr_state_open(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="OPEN\n", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_pr_state("https://x/1").state == "open"


def test_check_pr_state_unknown_on_failure(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=1, stdout="", stderr="not found")
    mgr = PRManager(mock_shell)
    assert mgr.check_pr_state("https://x/1").state == "unknown"


def test_check_pr_state_unknown_on_unexpected_output(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="DRAFT\n", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.check_pr_state("https://x/1").state == "unknown"


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
        ShellResult(returncode=0, stdout="", stderr=""),  # gh auth status → gh_usable=True
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
    assert "gh pr create" in mock_shell.run.call_args_list[1].args[0]
    assert "gh pr list" in mock_shell.run.call_args_list[2].args[0]


def test_create_pr_duplicate_but_list_fails_raises(mock_shell: MagicMock):
    from mship.core.pr import PRManager
    mock_shell.run.side_effect = [
        ShellResult(returncode=0, stdout="", stderr=""),  # gh auth status → gh_usable=True
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
    mock_shell.run.side_effect = [
        ShellResult(returncode=0, stdout="", stderr=""),  # gh auth status → gh_usable=True
        ShellResult(returncode=1, stdout="", stderr="fatal: some other error"),
    ]
    pr_mgr = PRManager(mock_shell)
    with pytest.raises(RuntimeError, match="some other error"):
        pr_mgr.create_pr(
            repo_path=Path("/repo"), branch="feat/x",
            title="t", body="b", base="main",
        )


def test_create_pr_falls_back_to_rest_on_graphql_rate_limit(mock_shell: MagicMock):
    """When gh pr create fails with GraphQL rate limit, fall back to REST."""
    from mship.core.pr import PRManager
    import json
    mock_shell.run.side_effect = [
        # 0. gh auth status → gh_usable=True
        ShellResult(returncode=0, stdout="", stderr=""),
        # 1. gh pr create hits the rate limit.
        ShellResult(
            returncode=1, stdout="",
            stderr="GraphQL: API rate limit already exceeded for user ID 1.",
        ),
        # 2. git remote get-url origin → https URL.
        ShellResult(
            returncode=0,
            stdout="https://github.com/org/repo.git\n",
            stderr="",
        ),
        # 3. gh api repos/org/repo/pulls -X POST → JSON body.
        ShellResult(
            returncode=0,
            stdout=json.dumps({"html_url": "https://github.com/org/repo/pull/99"}),
            stderr="",
        ),
    ]
    pr_mgr = PRManager(mock_shell)
    url = pr_mgr.create_pr(
        repo_path=Path("/repo"), branch="feat/x",
        title="T", body="B", base="main",
    )
    assert url == "https://github.com/org/repo/pull/99"
    # REST call is visible in the commands.
    cmds = [c.args[0] for c in mock_shell.run.call_args_list]
    assert any("gh api repos/org/repo/pulls" in c for c in cmds)


def test_create_pr_falls_back_on_secondary_rate_limit(mock_shell: MagicMock):
    """Secondary rate-limit signature also triggers REST fallback."""
    from mship.core.pr import PRManager
    import json
    mock_shell.run.side_effect = [
        ShellResult(returncode=0, stdout="", stderr=""),  # gh auth status → gh_usable=True
        ShellResult(
            returncode=1, stdout="",
            stderr="You have exceeded a secondary rate limit.",
        ),
        ShellResult(returncode=0, stdout="git@github.com:org/repo.git\n", stderr=""),
        ShellResult(
            returncode=0,
            stdout=json.dumps({"html_url": "https://github.com/org/repo/pull/7"}),
            stderr="",
        ),
    ]
    pr_mgr = PRManager(mock_shell)
    url = pr_mgr.create_pr(
        repo_path=Path("/repo"), branch="feat/x",
        title="T", body="B", base="main",
    )
    assert url == "https://github.com/org/repo/pull/7"


def test_create_pr_rest_fallback_fails_surfaces_original_error(mock_shell: MagicMock):
    """If REST also fails, surface the original GraphQL error, not the REST error."""
    from mship.core.pr import PRManager
    mock_shell.run.side_effect = [
        ShellResult(returncode=0, stdout="", stderr=""),  # gh auth status → gh_usable=True
        ShellResult(
            returncode=1, stdout="",
            stderr="GraphQL: API rate limit already exceeded for user ID 1.",
        ),
        ShellResult(returncode=0, stdout="https://github.com/org/repo.git\n", stderr=""),
        ShellResult(returncode=1, stdout="", stderr="rest api also broken"),
    ]
    pr_mgr = PRManager(mock_shell)
    with pytest.raises(RuntimeError) as exc_info:
        pr_mgr.create_pr(
            repo_path=Path("/repo"), branch="feat/x",
            title="T", body="B", base="main",
        )
    # Must include the ORIGINAL graphql signal so the user knows the cause.
    assert "rate limit" in str(exc_info.value).lower()


def test_create_pr_rest_fallback_parses_ssh_remote_url(mock_shell: MagicMock):
    """SSH-style git@github.com:owner/repo.git parses correctly."""
    from mship.core.pr import PRManager
    import json
    mock_shell.run.side_effect = [
        ShellResult(returncode=0, stdout="", stderr=""),  # gh auth status → gh_usable=True
        ShellResult(returncode=1, stdout="", stderr="GraphQL: API rate limit exceeded"),
        ShellResult(returncode=0, stdout="git@github.com:owner/repo.git\n", stderr=""),
        ShellResult(
            returncode=0,
            stdout=json.dumps({"html_url": "https://github.com/owner/repo/pull/1"}),
            stderr="",
        ),
    ]
    pr_mgr = PRManager(mock_shell)
    pr_mgr.create_pr(
        repo_path=Path("/repo"), branch="feat/x",
        title="T", body="B",
    )
    cmds = [c.args[0] for c in mock_shell.run.call_args_list]
    # Owner/repo extracted from ssh-style remote.
    assert any("repos/owner/repo/pulls" in c for c in cmds)


def test_create_pr_rest_fallback_parses_https_without_git_suffix(mock_shell: MagicMock):
    """HTTPS remote without .git suffix also parses."""
    from mship.core.pr import PRManager
    import json
    mock_shell.run.side_effect = [
        ShellResult(returncode=0, stdout="", stderr=""),  # gh auth status → gh_usable=True
        ShellResult(returncode=1, stdout="", stderr="GraphQL: API rate limit exceeded"),
        ShellResult(returncode=0, stdout="https://github.com/a/b\n", stderr=""),
        ShellResult(
            returncode=0,
            stdout=json.dumps({"html_url": "https://github.com/a/b/pull/3"}),
            stderr="",
        ),
    ]
    pr_mgr = PRManager(mock_shell)
    pr_mgr.create_pr(
        repo_path=Path("/repo"), branch="feat/x",
        title="T", body="B",
    )
    cmds = [c.args[0] for c in mock_shell.run.call_args_list]
    assert any("repos/a/b/pulls" in c for c in cmds)


# --- _classify_pr_state_reason + PrStateResult (issue #73) ---


@pytest.mark.parametrize("stderr,expected", [
    ("GraphQL: API rate limit exceeded for user ID 1", "rate limited"),
    ("You have exceeded a secondary rate limit", "rate limited"),
    ("authentication required; run 'gh auth login'", "gh not authenticated"),
    ("error: not logged in", "gh not authenticated"),
    ("could not resolve host: api.github.com", "network error"),
    ("connection timed out", "network error"),
    ("could not find pull request", "not found"),
    ("GraphQL: Could not resolve to a PullRequest", "not found"),
    ("HTTP 404: Not Found", "not found"),
])
def test_classify_pr_state_reason_signatures(stderr, expected):
    from mship.core.pr import _classify_pr_state_reason
    assert _classify_pr_state_reason(returncode=1, stderr=stderr, raw_state="") == expected


def test_classify_pr_state_reason_unmapped_state():
    from mship.core.pr import _classify_pr_state_reason
    reason = _classify_pr_state_reason(returncode=0, stderr="", raw_state="DRAFT")
    assert reason == "unmapped state: DRAFT"


def test_classify_pr_state_reason_gh_not_installed():
    from mship.core.pr import _classify_pr_state_reason
    assert _classify_pr_state_reason(returncode=127, stderr="", raw_state="") == "gh not installed"


def test_classify_pr_state_reason_other_excerpt():
    """Unmatched stderr falls into 'other: <80-char excerpt>'."""
    from mship.core.pr import _classify_pr_state_reason
    stderr = "some unexpected error message we haven't classified: very long " * 3
    reason = _classify_pr_state_reason(returncode=1, stderr=stderr, raw_state="")
    assert reason.startswith("other: ")
    assert len(reason) <= len("other: ") + 80


def test_check_pr_state_returns_pr_state_result_tuple(mock_shell):
    """Return value is a NamedTuple with .state and .reason."""
    from mship.core.pr import PRManager, PrStateResult
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="MERGED\n", stderr="")
    result = PRManager(mock_shell).check_pr_state("https://x/1")
    assert isinstance(result, PrStateResult)
    assert result.state == "merged"
    assert result.reason == ""


def test_check_pr_state_unknown_rate_limit_surfaces_reason(mock_shell):
    from mship.core.pr import PRManager
    mock_shell.run.return_value = ShellResult(
        returncode=1, stdout="",
        stderr="GraphQL: API rate limit exceeded for user ID 1",
    )
    result = PRManager(mock_shell).check_pr_state("https://x/1")
    assert result.state == "unknown"
    assert result.reason == "rate limited"


# --- Task 4: token-authed push + gh-or-httpx PR (MOS-187) ---


class _Shell:
    def __init__(self, gh_returncode=0):
        self.calls = []
        self._gh_rc = gh_returncode
    def run(self, command, cwd, env=None):
        self.calls.append((command, env))
        if command.startswith("gh auth status"):
            return ShellResult(returncode=self._gh_rc, stdout="", stderr="")
        if command.startswith("git remote get-url"):
            return ShellResult(returncode=0, stdout="https://github.com/o/r\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")


def test_push_branch_includes_cred_args_with_token():
    sh = _Shell()
    PRManager(sh).push_branch(Path("/x"), "feat/y", token="ghp_supersecrettoken99")
    cmd, env = next((c, e) for c, e in sh.calls if "git" in c and "push" in c)
    assert "credential.https://github.com.helper" in cmd
    assert "ghp_supersecrettoken99" not in cmd
    assert env and env["MSHIP_GH_TOKEN"] == "ghp_supersecrettoken99"


def test_gh_usable_true_when_status_zero():
    assert PRManager(_Shell(gh_returncode=0)).gh_usable() is True


def test_gh_usable_false_when_not_installed():
    assert PRManager(_Shell(gh_returncode=127)).gh_usable() is False


def test_create_pr_uses_httpx_when_gh_absent(monkeypatch):
    sh = _Shell(gh_returncode=127)
    sent = {}
    def fake_httpx(token, owner, repo, *, head, base, title, body, client=None):
        sent.update(owner=owner, repo=repo, head=head, base=base, token=token)
        return "https://github.com/o/r/pull/9"
    monkeypatch.setattr("mship.core.pr.create_pr_via_httpx", fake_httpx)
    url = PRManager(sh).create_pr(Path("/x"), "feat/y", "T", "B", base="main", token="tok")
    assert url == "https://github.com/o/r/pull/9"
    assert sent == {"owner": "o", "repo": "r", "head": "feat/y", "base": "main", "token": "tok"}


def test_create_pr_uses_gh_when_available():
    sh = _Shell(gh_returncode=0)
    real_run = sh.run
    def run(command, cwd, env=None):
        if command.startswith("gh pr create"):
            return ShellResult(returncode=0, stdout="https://github.com/o/r/pull/3\n", stderr="")
        return real_run(command, cwd, env)
    sh.run = run
    url = PRManager(sh).create_pr(Path("/x"), "feat/y", "T", "B", base="main", token="tok")
    assert url == "https://github.com/o/r/pull/3"
    assert any("gh auth status" in c for c, _ in sh.calls)


def test_create_pr_no_gh_no_token_raises():
    import pytest
    with pytest.raises(RuntimeError, match="gh CLI not available"):
        PRManager(_Shell(gh_returncode=127)).create_pr(Path("/x"), "feat/y", "T", "B", base="main")


def test_check_gh_available_populates_cache():
    # check_gh_available() runs `gh auth status`; it should seed the cache so a
    # later gh_usable() (e.g. inside create_pr) doesn't run it a second time.
    sh = _Shell(gh_returncode=0)
    mgr = PRManager(sh)
    mgr.check_gh_available()
    assert mgr.gh_usable() is True
    gh_calls = [c for c, _ in sh.calls if c.startswith("gh auth status")]
    assert len(gh_calls) == 1

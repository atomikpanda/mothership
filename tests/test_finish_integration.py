"""Integration test: spawn → finish creates PRs with coordination blocks."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def finish_workspace(workspace_with_git: Path):
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)

    def _default_run(cmd, cwd, env=None):
        if "ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/main\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    mock_shell.run.side_effect = _default_run
    container.shell.override(mock_shell)

    yield workspace_with_git, mock_shell
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_finish_single_repo_no_coordination_block(finish_workspace):
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "single repo test", "--repos", "shared"])
    assert result.exit_code == 0, result.output

    pr_url = "https://github.com/org/shared/pull/99"
    call_log = []

    def mock_run(cmd, cwd, env=None):
        call_log.append(cmd)
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/main\n", stderr="")
        if "rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout=f"{pr_url}\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish", "--task", "single-repo-test"])
    assert result.exit_code == 0, result.output

    mgr = StateManager(workspace / ".mothership")
    state = mgr.load()
    assert state.tasks["single-repo-test"].pr_urls["shared"] == pr_url

    # Single repo: no gh pr edit calls (no coordination block)
    edit_calls = [c for c in call_log if "gh pr edit" in c]
    assert len(edit_calls) == 0


def test_finish_multi_repo_adds_coordination(finish_workspace):
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "multi repo test", "--repos", "shared,auth-service"])
    assert result.exit_code == 0, result.output

    pr_counter = [0]
    call_log = []

    def mock_run(cmd, cwd, env=None):
        call_log.append(cmd)
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/main\n", stderr="")
        if "rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            pr_counter[0] += 1
            return ShellResult(returncode=0, stdout=f"https://github.com/org/repo/pull/{pr_counter[0]}\n", stderr="")
        if "gh pr view" in cmd:
            return ShellResult(returncode=0, stdout="original body", stderr="")
        if "gh pr edit" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish", "--task", "multi-repo-test"])
    assert result.exit_code == 0, result.output

    # Verify 2 PRs created
    create_calls = [c for c in call_log if "gh pr create" in c]
    assert len(create_calls) == 2

    # Verify coordination blocks added (gh pr edit called for each)
    edit_calls = [c for c in call_log if "gh pr edit" in c]
    assert len(edit_calls) == 2


def test_finish_idempotent_rerun(finish_workspace):
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "idempotent test", "--repos", "shared"])
    assert result.exit_code == 0, result.output

    # First finish
    def _first_finish_run(cmd, cwd, env=None):
        if "gh auth" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/main\n", stderr="")
        if "rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")
    mock_shell.run.side_effect = _first_finish_run

    result = runner.invoke(app, ["finish", "--task", "idempotent-test"])
    assert result.exit_code == 0, result.output

    # Second finish — should skip existing PR
    call_log = []
    original_side_effect = mock_shell.run.side_effect
    def tracking_run(cmd, cwd, env=None):
        call_log.append(cmd)
        if "gh auth" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = tracking_run

    result = runner.invoke(app, ["finish", "--task", "idempotent-test"])
    assert result.exit_code == 0, result.output

    # No gh pr create on second run
    create_calls = [c for c in call_log if "gh pr create" in c]
    assert len(create_calls) == 0


def test_finish_not_blocked_by_own_worktree(finish_workspace):
    """mship finish must not block on extra_worktrees from its own worktree."""
    workspace, mock_shell = finish_workspace

    # Spawn normally (with --force-audit since the mock shell doesn't actually
    # make audits clean, the point of this test is what finish does).
    result = runner.invoke(app, ["spawn", "own wt", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    # Simulate audit returning extra_worktrees iff the worktree is not known.
    # Track the command and assert git worktree list is invoked; since
    # finish_workspace uses a shell mock, we stub its responses for audit probes.
    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git fetch" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "symbolic-ref" in cmd:
            return ShellResult(returncode=0, stdout="main\n", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/main\n", stderr="")
        if "rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "git worktree list --porcelain" in cmd:
            # Report TWO worktrees: the main checkout AND the task's worktree.
            # The finish audit should exclude the task worktree.
            return ShellResult(
                returncode=0,
                stdout="worktree /tmp/shared\nworktree /tmp/shared-wt\n",
                stderr="",
            )
        if "git status --porcelain" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc\trefs/heads/main\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    # Point the state's worktree entry at /tmp/shared-wt so the exclusion matches
    # what the mocked `git worktree list` reports.
    state_path = workspace / ".mothership" / "state.yaml"
    import yaml
    data = yaml.safe_load(state_path.read_text())
    slug = "own-wt"
    data["tasks"][slug]["worktrees"] = {"shared": "/tmp/shared-wt"}
    state_path.write_text(yaml.safe_dump(data))
    from mship.cli import container
    container.state_manager.reset()

    result = runner.invoke(app, ["finish", "--task", slug])
    assert result.exit_code == 0, result.output
    assert "extra_worktrees" not in result.output


def test_finish_passes_base_from_config(finish_workspace, tmp_path):
    """Config base_branch flows into gh pr create --base."""
    import yaml

    workspace, mock_shell = finish_workspace

    # Rewrite config to set base_branch on `shared`
    cfg_path = workspace / "mothership.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["repos"]["shared"]["base_branch"] = "release/7"
    cfg_path.write_text(yaml.safe_dump(cfg))
    container.config.reset()

    result = runner.invoke(app, ["spawn", "base test", "--repos", "shared"])
    assert result.exit_code == 0, result.output

    call_log: list[str] = []

    def mock_run(cmd, cwd, env=None):
        call_log.append(cmd)
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc\trefs/heads/release/7\n", stderr="")
        if "git rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "git rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish", "--task", "base-test"])
    assert result.exit_code == 0, result.output

    create_calls = [c for c in call_log if "gh pr create" in c]
    assert len(create_calls) == 1
    assert "--base" in create_calls[0]
    assert "release/7" in create_calls[0]


def test_finish_fails_when_base_missing_on_remote(finish_workspace):
    import yaml

    workspace, mock_shell = finish_workspace
    cfg_path = workspace / "mothership.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["repos"]["shared"]["base_branch"] = "nope"
    cfg_path.write_text(yaml.safe_dump(cfg))
    container.config.reset()

    result = runner.invoke(app, ["spawn", "missing base", "--repos", "shared"])
    assert result.exit_code == 0, result.output

    pushed: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")  # empty → missing
        if "git push" in cmd:
            pushed.append(cmd)
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish", "--task", "missing-base"])
    assert result.exit_code != 0
    assert "nope" in result.output.lower() or "base" in result.output.lower()
    assert pushed == [], "no repo should be pushed when a base is missing"


def test_finish_blocks_when_affected_repo_is_dirty(finish_workspace):
    """Dirty affected repo blocks finish under default block_finish=true.

    Note: shared must have local commits (rev-list count > 0) so it's in the
    audit scope per #112 — drift in repos with no pending work no longer blocks.
    """
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "finish gate", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    def mock_run(cmd, cwd, env=None):
        if "git status --porcelain" in cmd:
            return ShellResult(returncode=0, stdout=" M foo.py\n", stderr="")
        if "git fetch" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "symbolic-ref" in cmd:
            return ShellResult(returncode=0, stdout="main\n", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/main\n", stderr="")
        if "rev-list --count" in cmd:
            # shared has 1 commit past origin/main → in audit scope.
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "worktree list" in cmd:
            return ShellResult(returncode=0, stdout="worktree /tmp/shared\n", stderr="")
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish", "--task", "finish-gate"])
    assert result.exit_code == 1
    assert "dirty_worktree" in result.output


def test_finish_unrelated_dirty_repo_does_not_block(finish_workspace):
    """Drift in a repo not in task.affected_repos must not block finish."""
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "unrelated test", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0

    dirty_repos = {"auth-service"}  # NOT in affected_repos

    def mock_run(cmd, cwd, env=None):
        if "git status --porcelain" in cmd and any(str(cwd).endswith(d) for d in dirty_repos):
            return ShellResult(returncode=0, stdout=" M foo.py\n", stderr="")
        if "git status --porcelain" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git fetch" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "symbolic-ref" in cmd:
            return ShellResult(returncode=0, stdout="main\n", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/main\n", stderr="")
        if "rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "worktree list" in cmd:
            return ShellResult(returncode=0, stdout="worktree /tmp/r\n", stderr="")
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc\trefs/heads/main\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish", "--task", "unrelated-test"])
    assert result.exit_code == 0, result.output


def test_finish_skips_untouched_repos_in_multi_repo_task(finish_workspace):
    """Finish should skip repos with 0 commits ahead, creating PRs only for the
    others. See #83."""
    import yaml

    workspace, mock_shell = finish_workspace
    cfg_path = workspace / "mothership.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["repos"]["shared"]["base_branch"] = "main"
    cfg["repos"]["auth-service"]["base_branch"] = "main"
    cfg_path.write_text(yaml.safe_dump(cfg))
    container.config.reset()

    result = runner.invoke(
        app, ["spawn", "partial work", "--repos", "shared,auth-service", "--force-audit"]
    )
    assert result.exit_code == 0, result.output

    create_calls: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(
                returncode=0, stdout="abc\trefs/heads/main\n", stderr="",
            )
        if "git rev-list --count" in cmd:
            # auth-service is untouched; shared has commits.
            if "auth-service" in str(cwd):
                return ShellResult(returncode=0, stdout="0\n", stderr="")
            return ShellResult(returncode=0, stdout="2\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            create_calls.append(str(cwd))
            return ShellResult(
                returncode=0, stdout="https://github.com/org/repo/pull/1\n",
                stderr="",
            )
        if "gh pr list" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish", "--task", "partial-work", "--force-audit"])
    assert result.exit_code == 0, result.output
    # auth-service was untouched → no PR created there.
    assert not any("auth-service" in c for c in create_calls), create_calls
    # shared had commits → exactly 1 PR.
    assert any("shared" in c for c in create_calls), create_calls
    assert len(create_calls) == 1
    # Output should name the skipped repo.
    assert "auth-service" in result.output
    assert "no commits" in result.output.lower() or "skipped" in result.output.lower()


def test_finish_fails_when_branch_has_no_commits(finish_workspace):
    """Empty feature branch (no commits past base) must be caught pre-push."""
    import yaml

    workspace, mock_shell = finish_workspace
    cfg_path = workspace / "mothership.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["repos"]["shared"]["base_branch"] = "main"
    cfg_path.write_text(yaml.safe_dump(cfg))
    from mship.cli import container
    container.config.reset()

    result = runner.invoke(app, ["spawn", "empty branch", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    pushed: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc\trefs/heads/main\n", stderr="")
        if "git rev-list --count" in cmd:
            # Empty — feature branch has no commits past origin/main.
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "git push" in cmd:
            pushed.append(cmd)
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish", "--task", "empty-branch"])
    assert result.exit_code != 0
    assert "no commits" in result.output.lower() or "No commits to push" in result.output
    assert pushed == [], "no repo should be pushed when a branch is empty"


def test_finish_stamps_finished_at(finish_workspace):
    workspace, mock_shell = finish_workspace

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc\trefs/heads/main\n", stderr="")
        if "git rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "git rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["spawn", "stamp test", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["finish", "--task", "stamp-test"])
    assert result.exit_code == 0, result.output
    assert "mship close" in result.output

    state = StateManager(workspace / ".mothership").load()
    assert state.tasks["stamp-test"].finished_at is not None


def test_finish_push_only_skips_gh_pr_create(finish_workspace):
    workspace, mock_shell = finish_workspace
    push_calls: list[str] = []
    pr_calls: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "gh pr create" in cmd:
            pr_calls.append(cmd)
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        if "git push" in cmd:
            push_calls.append(cmd)
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "git rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["spawn", "push only", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["finish", "--push-only", "--task", "push-only"])
    assert result.exit_code == 0, result.output
    assert len(push_calls) == 1
    assert pr_calls == []
    assert "mship close" in result.output
    assert "Branch pushed" in result.output

    state = StateManager(workspace / ".mothership").load()
    task = state.tasks["push-only"]
    assert task.finished_at is not None
    assert task.pr_urls == {}


def test_finish_push_only_rejects_base_flags(finish_workspace):
    workspace, mock_shell = finish_workspace
    mock_shell.run.side_effect = lambda cmd, cwd, env=None: ShellResult(returncode=0, stdout="", stderr="")

    result = runner.invoke(app, ["spawn", "conflict flags", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0

    result = runner.invoke(app, ["finish", "--push-only", "--base", "main"])
    assert result.exit_code != 0
    assert "push-only" in result.output.lower()


def test_finish_suppresses_no_upstream_for_task_branch(finish_workspace):
    """Regression for #6: finish must succeed when the only audit error is
    `no_upstream` on the task's own branch — finish itself creates the upstream."""
    workspace, mock_shell = finish_workspace

    # Spawn without audit gate interference (the repo has no origin configured
    # in the fixture, so audit would fire no_upstream at spawn time too).
    result = runner.invoke(app, ["spawn", "noupstream fix", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    # Mock shell to simulate a clean worktree on the task branch with no
    # upstream configured. The key: when finish audits, `no_upstream` fires on
    # feat/noupstream-fix. The fix under test removes it from the gate.
    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git status --porcelain" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git fetch" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "symbolic-ref --short HEAD" in cmd:
            return ShellResult(returncode=0, stdout="feat/noupstream-fix\n", stderr="")
        if "git rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            # No upstream -> non-zero -> audit emits no_upstream
            return ShellResult(returncode=128, stdout="", stderr="no upstream")
        if "git rev-parse --git-common-dir" in cmd:
            return ShellResult(returncode=0, stdout=".git\n", stderr="")
        if "git worktree list" in cmd:
            return ShellResult(returncode=0, stdout="worktree /tmp/shared\n", stderr="")
        if "git rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "git rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc\trefs/heads/main\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    # NO --force-audit — this is the whole point of the fix.
    result = runner.invoke(app, ["finish", "--task", "noupstream-fix"])
    assert result.exit_code == 0, result.output
    assert "BYPASSED AUDIT" not in result.output


def test_finish_still_blocks_other_audit_errors(finish_workspace):
    """The fix must only suppress no_upstream; dirty_worktree etc. still block.

    Note: shared must have local commits (count > 0) so it's in the audit scope
    per #112 — drift in repos with no pending work is no longer a blocking error.
    """
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "still blocks", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git status --porcelain" in cmd:
            # Dirty worktree → still blocks
            return ShellResult(returncode=0, stdout=" M foo.py\n", stderr="")
        if "git fetch" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "symbolic-ref --short HEAD" in cmd:
            return ShellResult(returncode=0, stdout="feat/still-blocks\n", stderr="")
        if "git rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=128, stdout="", stderr="")
        if "git rev-parse --git-common-dir" in cmd:
            return ShellResult(returncode=0, stdout=".git\n", stderr="")
        if "git worktree list" in cmd:
            return ShellResult(returncode=0, stdout="worktree /tmp/shared\n", stderr="")
        if "git rev-list --count" in cmd:
            # shared has 1 commit past origin/main → in audit scope.
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish", "--task", "still-blocks"])
    assert result.exit_code != 0
    assert "dirty_worktree" in result.output


def test_finish_does_not_block_on_drift_in_unrelated_repo(finish_workspace):
    """Drift in a repo without local commits should NOT block finish (#112).

    The user's reported scenario: an unrelated repo is `behind_remote` but has
    no commits on the task branch. Previously this blocked finish; now it
    should be silently filtered out of the audit-blocking scope while
    affected_repos with actual commits still get audited.
    """
    workspace, mock_shell = finish_workspace

    # Spawn affecting shared (will have commits) and api-gateway (untouched).
    # api-gateway depends on shared, but shared has no upstream deps so the
    # scope of "shared has commits" is just {shared} — api-gateway is excluded.
    result = runner.invoke(app, ["spawn", "scoped finish", "--repos", "shared,auth-service,api-gateway", "--force-audit"])
    assert result.exit_code == 0

    def mock_run(cmd, cwd, env=None):
        cwd_str = str(cwd) if cwd else ""
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://x/pr/1\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/main\n", stderr="")
        if "git fetch" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "symbolic-ref --short HEAD" in cmd:
            return ShellResult(returncode=0, stdout="feat/scoped-finish\n", stderr="")
        if "git rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=128, stdout="", stderr="")
        if "git rev-parse --git-common-dir" in cmd:
            return ShellResult(returncode=0, stdout=".git\n", stderr="")
        if "git worktree list" in cmd:
            return ShellResult(returncode=0, stdout="worktree /tmp\n", stderr="")
        if "git status --porcelain" in cmd:
            # api-gateway is dirty — but it has no commits on this task, so
            # it's out of audit scope and the dirty state should NOT block.
            if "api-gateway" in cwd_str:
                return ShellResult(returncode=0, stdout=" M unrelated.py\n", stderr="")
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git rev-list --count" in cmd:
            # shared has commits; api-gateway does not.
            if "shared" in cwd_str:
                return ShellResult(returncode=0, stdout="1\n", stderr="")
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(
        app,
        ["finish", "--task", "scoped-finish", "--body", "## Summary\nx\n## Test plan\n- [x] manual"],
    )
    # Tailrd's dirty_worktree is out of scope (no commits) → does not block.
    # Shared has commits and clean state → finish proceeds.
    assert result.exit_code == 0, result.output
    assert "blocked by audit" not in result.output


def test_finish_auto_links_issue_refs_in_description(finish_workspace):
    """Regression for #8: task description containing `#N` should produce PR body with `Closes #N`."""
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "fix #42 something important", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    captured_body: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git status --porcelain" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "symbolic-ref --short HEAD" in cmd:
            return ShellResult(returncode=0, stdout="feat/fix-42-something-important\n", stderr="")
        if "git fetch" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/feat/fix-42-something-important\n", stderr="")
        if "rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "git worktree list" in cmd:
            return ShellResult(returncode=0, stdout="worktree /tmp/shared\n", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc\trefs/heads/main\n", stderr="")
        if "git log --format=%s" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            # Extract --body from the command
            import shlex as _shlex
            tokens = _shlex.split(cmd)
            if "--body" in tokens:
                idx = tokens.index("--body")
                captured_body.append(tokens[idx + 1])
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish", "--task", "fix-42-something-important"])
    assert result.exit_code == 0, result.output
    assert captured_body, "expected gh pr create to be invoked"
    assert "Closes #42" in captured_body[0]


def test_finish_pr_body_unchanged_when_no_issue_refs(finish_workspace):
    """Task description without `#N` → PR body is just the description."""
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "ordinary task description", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    captured_body: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/main\n", stderr="")
        if "rev-list --count" in cmd and "origin/" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "git log --format=%s" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            import shlex as _shlex
            tokens = _shlex.split(cmd)
            if "--body" in tokens:
                idx = tokens.index("--body")
                captured_body.append(tokens[idx + 1])
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish", "--force-audit", "--task", "ordinary-task-description"])
    assert result.exit_code == 0, result.output
    assert captured_body
    assert "Closes" not in captured_body[0]
    assert captured_body[0] == "ordinary task description"


def test_finish_warns_when_no_test_evidence_default(finish_workspace):
    """Default: missing test evidence is a WARNING (not a block). See #81."""
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "no evidence", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/main\n", stderr="")
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish", "--task", "no-evidence", "--force-audit"])
    # Finish still succeeds; the warning is advisory.
    assert result.exit_code == 0, result.output
    # Warning text references the evidence gap.
    lower = result.output.lower()
    assert "test" in lower and ("not run" in lower or "missing" in lower or "evidence" in lower)


def test_finish_blocks_when_require_tests_and_no_evidence(finish_workspace):
    """--require-tests escalates missing evidence to a BLOCK. See #81."""
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "require block", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    pushed: list[str] = []
    prs: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/main\n", stderr="")
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "git push" in cmd:
            pushed.append(cmd)
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            prs.append(cmd)
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(
        app, ["finish", "--task", "require-block", "--force-audit", "--require-tests"]
    )
    assert result.exit_code != 0
    assert pushed == [], "finish must block before pushing when --require-tests"
    assert prs == [], "finish must block before creating PRs when --require-tests"
    assert "require-tests" in result.output.lower() or "blocking" in result.output.lower()


def test_finish_evidence_via_journal_suppresses_warning(finish_workspace, tmp_path):
    """Journal `test-state=pass` counts as evidence — no warning. See #81."""
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "journal evidence", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    # Record journal evidence for the task (global scope — applies to all repos).
    from mship.cli import container as cli_container
    log_mgr = cli_container.log_manager()
    log_mgr.append("journal-evidence", "ran pytest", test_state="pass")

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc123\trefs/heads/main\n", stderr="")
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="1\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish", "--task", "journal-evidence", "--force-audit"])
    assert result.exit_code == 0, result.output
    # No "not run" / "missing" / "evidence" warning.
    lower = result.output.lower()
    assert "test-evidence warnings" not in lower
    assert "not run" not in lower

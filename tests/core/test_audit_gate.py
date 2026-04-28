from pathlib import Path

import pytest

from mship.core.audit_gate import run_audit_gate, AuditGateBlocked
from mship.core.repo_state import AuditReport, RepoAudit, Issue


def _blocking_report():
    return AuditReport(repos=(
        RepoAudit(name="cli", path=Path("/"), current_branch="main",
                  issues=(Issue("dirty_worktree", "error", "x"),)),
    ))


def _clean_report():
    return AuditReport(repos=(
        RepoAudit(name="cli", path=Path("/"), current_branch="main", issues=()),
    ))


def test_gate_passes_when_no_errors():
    run_audit_gate(_clean_report(), block=True, force=False, command_name="spawn",
                   on_bypass=lambda codes: None)


def test_gate_blocks_when_errors_and_block_true():
    with pytest.raises(AuditGateBlocked) as exc:
        run_audit_gate(_blocking_report(), block=True, force=False, command_name="spawn",
                       on_bypass=lambda codes: None)
    assert "dirty_worktree" in str(exc.value)


def test_gate_force_calls_on_bypass_and_proceeds():
    seen: list[list[str]] = []
    run_audit_gate(_blocking_report(), block=True, force=True, command_name="spawn",
                   on_bypass=lambda codes: seen.append(list(codes)))
    assert seen == [["cli:dirty_worktree"]]


def test_gate_warns_but_does_not_block_when_block_false():
    run_audit_gate(_blocking_report(), block=False, force=False, command_name="spawn",
                   on_bypass=lambda codes: None)


from mship.core.audit_gate import collect_known_worktree_paths


class _FakeTask:
    def __init__(self, worktrees: dict[str, str]):
        self.worktrees = worktrees


class _FakeState:
    def __init__(self, tasks):
        self.tasks = tasks


class _FakeStateMgr:
    def __init__(self, state):
        self._state = state

    def load(self):
        return self._state


def test_collect_known_worktree_paths_union_across_tasks(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "c").mkdir()
    state = _FakeState({
        "task1": _FakeTask({"cli": str(tmp_path / "a"), "api": str(tmp_path / "b")}),
        "task2": _FakeTask({"cli": str(tmp_path / "c")}),
    })
    result = collect_known_worktree_paths(_FakeStateMgr(state))
    expected = frozenset({
        (tmp_path / "a").resolve(),
        (tmp_path / "b").resolve(),
        (tmp_path / "c").resolve(),
    })
    assert result == expected


def test_collect_known_worktree_paths_no_tasks():
    state = _FakeState({})
    result = collect_known_worktree_paths(_FakeStateMgr(state))
    assert result == frozenset()


def _two_repo_blocking_report():
    """Report with one error in 'cli' and one error in 'unrelated'."""
    return AuditReport(repos=(
        RepoAudit(name="cli", path=Path("/"), current_branch="main",
                  issues=(Issue("dirty_worktree", "error", "x"),)),
        RepoAudit(name="unrelated", path=Path("/"), current_branch="main",
                  issues=(Issue("behind_remote", "error", "2 commits behind"),)),
    ))


def test_gate_scope_repos_filters_out_of_scope_errors():
    """Errors in repos outside scope_repos do not block. See #112."""
    # Only 'unrelated' has an error; 'cli' is clean.
    report = AuditReport(repos=(
        RepoAudit(name="cli", path=Path("/"), current_branch="main", issues=()),
        RepoAudit(name="unrelated", path=Path("/"), current_branch="main",
                  issues=(Issue("behind_remote", "error", "2 commits behind"),)),
    ))
    run_audit_gate(
        report,
        block=True, force=False, command_name="finish",
        on_bypass=lambda codes: None,
        scope_repos=frozenset({"cli"}),  # 'unrelated' is out of scope
    )
    # No exception — the only error is in 'unrelated', which is out of scope.


def test_gate_scope_repos_still_blocks_on_in_scope_error():
    """Errors in repos that ARE in scope_repos still block."""
    with pytest.raises(AuditGateBlocked) as exc:
        run_audit_gate(
            _two_repo_blocking_report(),
            block=True, force=False, command_name="finish",
            on_bypass=lambda codes: None,
            scope_repos=frozenset({"cli", "unrelated"}),  # both in scope
        )
    assert "dirty_worktree" in str(exc.value)
    # 'unrelated' is in scope so its error also blocks.
    assert "behind_remote" in str(exc.value)


def test_gate_scope_repos_force_bypass_only_emits_in_scope_codes():
    """With --force-audit, on_bypass receives only in-scope codes."""
    seen: list[list[str]] = []
    run_audit_gate(
        _two_repo_blocking_report(),
        block=True, force=True, command_name="finish",
        on_bypass=lambda codes: seen.append(list(codes)),
        scope_repos=frozenset({"cli"}),  # 'unrelated' excluded
    )
    # The bypass log only mentions cli's error, not unrelated's.
    assert seen == [["cli:dirty_worktree"]]


def test_gate_scope_repos_none_unchanged_behavior():
    """scope_repos=None (default) preserves current behavior — all errors block."""
    with pytest.raises(AuditGateBlocked) as exc:
        run_audit_gate(
            _two_repo_blocking_report(),
            block=True, force=False, command_name="finish",
            on_bypass=lambda codes: None,
            # scope_repos not passed → defaults to None
        )
    assert "dirty_worktree" in str(exc.value)
    assert "behind_remote" in str(exc.value)


def test_compute_finish_audit_scope_includes_repos_with_commits(tmp_path: Path):
    """Repos in affected_repos with local commits past their base are in scope. See #112."""
    from mship.core.audit_gate import compute_finish_audit_scope
    from unittest.mock import MagicMock

    task = MagicMock()
    task.affected_repos = ["cli"]
    task.worktrees = {"cli": str(tmp_path / "cli")}
    task.branch = "feat/x"
    (tmp_path / "cli").mkdir()

    config = MagicMock()
    config.repos = {"cli": MagicMock(base_branch="main")}

    graph = MagicMock()
    graph.dependencies.return_value = []  # no deps

    pr_mgr = MagicMock()
    pr_mgr.count_commits_ahead.return_value = 3  # has commits

    scope = compute_finish_audit_scope(task, config, graph, pr_mgr)
    assert scope == frozenset({"cli"})


def test_compute_finish_audit_scope_excludes_repos_without_commits(tmp_path: Path):
    """Repos in affected_repos without local commits are NOT in scope (no PR to push)."""
    from mship.core.audit_gate import compute_finish_audit_scope
    from unittest.mock import MagicMock

    task = MagicMock()
    task.affected_repos = ["cli", "untouched"]
    task.worktrees = {"cli": str(tmp_path / "cli"), "untouched": str(tmp_path / "untouched")}
    task.branch = "feat/x"
    (tmp_path / "cli").mkdir()
    (tmp_path / "untouched").mkdir()

    config = MagicMock()
    config.repos = {
        "cli": MagicMock(base_branch="main"),
        "untouched": MagicMock(base_branch="main"),
    }

    graph = MagicMock()
    graph.dependencies.return_value = []

    pr_mgr = MagicMock()
    # cli has commits, untouched doesn't.
    pr_mgr.count_commits_ahead.side_effect = lambda path, base, branch: 3 if "cli" in str(path) else 0

    scope = compute_finish_audit_scope(task, config, graph, pr_mgr)
    assert scope == frozenset({"cli"})  # 'untouched' is excluded


def test_compute_finish_audit_scope_includes_transitive_deps(tmp_path: Path):
    """Transitive deps of repos with commits are also in scope (drift in deps could break build)."""
    from mship.core.audit_gate import compute_finish_audit_scope
    from unittest.mock import MagicMock

    task = MagicMock()
    task.affected_repos = ["api"]
    task.worktrees = {"api": str(tmp_path / "api")}
    task.branch = "feat/x"
    (tmp_path / "api").mkdir()

    config = MagicMock()
    config.repos = {"api": MagicMock(base_branch="main")}

    graph = MagicMock()
    graph.dependencies.return_value = ["shared", "auth"]  # api depends on shared + auth

    pr_mgr = MagicMock()
    pr_mgr.count_commits_ahead.return_value = 2  # api has commits

    scope = compute_finish_audit_scope(task, config, graph, pr_mgr)
    assert scope == frozenset({"api", "shared", "auth"})


def test_compute_finish_audit_scope_falls_back_to_main_when_no_base_branch(tmp_path: Path):
    """When repo config has no base_branch, fall back to 'main'."""
    from mship.core.audit_gate import compute_finish_audit_scope
    from unittest.mock import MagicMock

    task = MagicMock()
    task.affected_repos = ["cli"]
    task.worktrees = {"cli": str(tmp_path / "cli")}
    task.branch = "feat/x"
    (tmp_path / "cli").mkdir()

    config = MagicMock()
    cli_cfg = MagicMock()
    cli_cfg.base_branch = None  # not configured
    config.repos = {"cli": cli_cfg}

    graph = MagicMock()
    graph.dependencies.return_value = []

    pr_mgr = MagicMock()
    pr_mgr.count_commits_ahead.return_value = 1

    compute_finish_audit_scope(task, config, graph, pr_mgr)
    # Verify pr_mgr was called with 'main' as the base.
    args, kwargs = pr_mgr.count_commits_ahead.call_args
    # signature: count_commits_ahead(repo_path, base, branch)
    assert "main" in args or kwargs.get("base") == "main"


def test_compute_finish_audit_scope_skips_repos_without_worktree(tmp_path: Path):
    """Repos with no worktree path (or non-existent path) are not audited — silently excluded."""
    from mship.core.audit_gate import compute_finish_audit_scope
    from unittest.mock import MagicMock

    task = MagicMock()
    task.affected_repos = ["cli", "missing"]
    task.worktrees = {"cli": str(tmp_path / "cli")}  # no entry for 'missing'
    task.branch = "feat/x"
    (tmp_path / "cli").mkdir()

    config = MagicMock()
    config.repos = {
        "cli": MagicMock(base_branch="main"),
        "missing": MagicMock(base_branch="main"),
    }

    graph = MagicMock()
    graph.dependencies.return_value = []

    pr_mgr = MagicMock()
    pr_mgr.count_commits_ahead.return_value = 2

    scope = compute_finish_audit_scope(task, config, graph, pr_mgr)
    assert "missing" not in scope


def test_gate_does_not_block_when_only_warn_issues():
    """A repo audit with only `dirty_untracked` (warn) must not trip the gate."""
    audit = RepoAudit(
        name="cli", path=Path("/abs"), current_branch="main",
        issues=(Issue("dirty_untracked", "warn", "1 untracked file"),),
    )
    report = AuditReport(repos=(audit,))
    assert report.has_errors is False

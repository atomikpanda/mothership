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


def test_gate_does_not_block_when_only_warn_issues():
    """A repo audit with only `dirty_untracked` (warn) must not trip the gate."""
    audit = RepoAudit(
        name="cli", path=Path("/abs"), current_branch="main",
        issues=(Issue("dirty_untracked", "warn", "1 untracked file"),),
    )
    report = AuditReport(repos=(audit,))
    assert report.has_errors is False

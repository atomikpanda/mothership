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

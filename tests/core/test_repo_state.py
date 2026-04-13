from pathlib import Path

from mship.core.repo_state import Issue, RepoAudit, AuditReport


def test_issue_is_immutable():
    i = Issue(code="dirty_worktree", severity="error", message="x")
    try:
        i.code = "other"
        raised = False
    except Exception:
        raised = True
    assert raised


def test_repo_audit_has_errors_true_for_error_issue():
    a = RepoAudit(
        name="cli",
        path=Path("/abs"),
        current_branch="main",
        issues=(Issue(code="dirty_worktree", severity="error", message="x"),),
    )
    assert a.has_errors is True


def test_repo_audit_has_errors_false_for_info_only():
    a = RepoAudit(
        name="cli",
        path=Path("/abs"),
        current_branch="main",
        issues=(Issue(code="ahead_remote", severity="info", message="x"),),
    )
    assert a.has_errors is False


def test_audit_report_has_errors_aggregates():
    clean = RepoAudit(name="a", path=Path("/"), current_branch="main", issues=())
    bad = RepoAudit(
        name="b", path=Path("/"), current_branch="main",
        issues=(Issue(code="dirty_worktree", severity="error", message="x"),),
    )
    assert AuditReport(repos=(clean,)).has_errors is False
    assert AuditReport(repos=(clean, bad)).has_errors is True


def test_audit_report_to_json_shape():
    r = AuditReport(repos=(
        RepoAudit(
            name="cli", path=Path("/abs/cli"), current_branch="main",
            issues=(Issue(code="dirty_worktree", severity="error", message="3 files"),),
        ),
    ))
    payload = r.to_json(workspace="ws")
    assert payload["workspace"] == "ws"
    assert payload["has_errors"] is True
    assert payload["repos"] == [{
        "name": "cli",
        "path": "/abs/cli",
        "current_branch": "main",
        "issues": [{"code": "dirty_worktree", "severity": "error", "message": "3 files"}],
    }]

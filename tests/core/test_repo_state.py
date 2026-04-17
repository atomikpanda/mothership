import os
import subprocess
from pathlib import Path

import pytest
import yaml

from mship.core.config import ConfigLoader
from mship.core.repo_state import Issue, RepoAudit, AuditReport, audit_repos
from mship.util.shell import ShellRunner


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


# ---------------------------------------------------------------------------
# Task 3: audit_repos detection tests (require audit_workspace fixture)
# ---------------------------------------------------------------------------


def _load(ws: Path):
    return ConfigLoader.load(ws / "mothership.yaml"), ShellRunner()


def _sh(*args, cwd):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, env=env)


def _issue_codes(report, name):
    (repo,) = [r for r in report.repos if r.name == name]
    return {i.code for i in repo.issues}


def test_audit_clean_repos_have_no_issues(audit_workspace):
    cfg, shell = _load(audit_workspace)
    rep = audit_repos(cfg, shell)
    assert rep.has_errors is False
    for r in rep.repos:
        assert r.issues == ()


def test_audit_path_missing(audit_workspace):
    cfg, shell = _load(audit_workspace)
    (audit_workspace / "cli").rename(audit_workspace / "cli.moved")
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "path_missing" in _issue_codes(rep, "cli")


def test_audit_not_a_git_repo(audit_workspace):
    cfg, shell = _load(audit_workspace)
    import shutil
    shutil.rmtree(audit_workspace / "cli" / ".git")
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "not_a_git_repo" in _issue_codes(rep, "cli")


def test_audit_detached_head(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    _sh("git", "checkout", "--detach", "HEAD", cwd=clone)
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "detached_head" in _issue_codes(rep, "cli")


def test_audit_unexpected_branch(audit_workspace):
    cfg_path = audit_workspace / "mothership.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    data["repos"]["cli"]["expected_branch"] = "marshal-refactor"
    cfg_path.write_text(yaml.safe_dump(data))
    cfg, shell = _load(audit_workspace)
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "unexpected_branch" in _issue_codes(rep, "cli")


def test_audit_unexpected_branch_main_checkout_message(audit_workspace):
    """The unexpected_branch error should reference the main checkout, not cwd."""
    cfg_path = audit_workspace / "mothership.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    data["repos"]["cli"]["expected_branch"] = "marshal-refactor"
    cfg_path.write_text(yaml.safe_dump(data))
    cfg, shell = _load(audit_workspace)
    rep = audit_repos(cfg, shell, names=["cli"])
    cli = next(r for r in rep.repos if r.name == "cli")
    msgs = [i.message for i in cli.issues if i.code == "unexpected_branch"]
    assert msgs and "main checkout" in msgs[0]


def test_audit_expected_branch_passes_when_root_path_is_a_worktree(audit_workspace):
    """Issue #3: audit from a worktree path must still see the main checkout's branch."""
    import yaml as _yaml
    clone = audit_workspace / "cli"
    wt = audit_workspace / "cli-feat-wt"
    _sh("git", "worktree", "add", str(wt), "-b", "feat/x", cwd=clone)

    # Point the config's cli.path at the worktree, simulating the user's cwd-based
    # invocation from inside a feature-branch worktree.
    cfg_path = audit_workspace / "mothership.yaml"
    data = _yaml.safe_load(cfg_path.read_text())
    data["repos"]["cli"]["path"] = str(wt)
    data["repos"]["cli"]["expected_branch"] = "main"   # main checkout IS on main
    cfg_path.write_text(_yaml.safe_dump(data))

    cfg, shell = _load(audit_workspace)
    rep = audit_repos(cfg, shell, names=["cli"])
    cli = next(r for r in rep.repos if r.name == "cli")
    codes = {i.code for i in cli.issues}
    # main checkout is on `main`, expected_branch is `main` → must NOT fire
    assert "unexpected_branch" not in codes


def test_audit_dirty_worktree(audit_workspace):
    cfg, shell = _load(audit_workspace)
    (audit_workspace / "cli" / "README.md").write_text("modified\n")
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "dirty_worktree" in _issue_codes(rep, "cli")


def test_audit_allow_dirty_suppresses(audit_workspace):
    cfg_path = audit_workspace / "mothership.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    data["repos"]["cli"]["allow_dirty"] = True
    cfg_path.write_text(yaml.safe_dump(data))
    cfg, shell = _load(audit_workspace)
    (audit_workspace / "cli" / "README.md").write_text("modified\n")  # tracked-modified
    (audit_workspace / "cli" / "new.txt").write_text("hi\n")          # untracked
    rep = audit_repos(cfg, shell, names=["cli"])
    codes = _issue_codes(rep, "cli")
    assert "dirty_worktree" not in codes
    assert "dirty_untracked" not in codes  # allow_dirty suppresses both tiers


def test_audit_ahead_remote_is_info(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    (clone / "x.txt").write_text("x")
    _sh("git", "add", "x.txt", cwd=clone)
    _sh("git", "commit", "-qm", "ahead", cwd=clone)
    rep = audit_repos(cfg, shell, names=["cli"])
    (repo,) = [r for r in rep.repos if r.name == "cli"]
    codes = {(i.code, i.severity) for i in repo.issues}
    assert ("ahead_remote", "info") in codes
    assert repo.has_errors is False


def test_audit_behind_remote(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    (clone / "y.txt").write_text("y")
    _sh("git", "add", "y.txt", cwd=clone)
    _sh("git", "commit", "-qm", "later", cwd=clone)
    _sh("git", "push", "-q", "origin", "main", cwd=clone)
    _sh("git", "reset", "--hard", "HEAD^", cwd=clone)
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "behind_remote" in _issue_codes(rep, "cli")


def test_audit_diverged(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    (clone / "a.txt").write_text("a")
    _sh("git", "add", "a.txt", cwd=clone)
    _sh("git", "commit", "-qm", "a", cwd=clone)
    _sh("git", "push", "-q", "origin", "main", cwd=clone)
    _sh("git", "reset", "--hard", "HEAD^", cwd=clone)
    (clone / "b.txt").write_text("b")
    _sh("git", "add", "b.txt", cwd=clone)
    _sh("git", "commit", "-qm", "b", cwd=clone)
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "diverged" in _issue_codes(rep, "cli")


def test_audit_no_upstream(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    _sh("git", "checkout", "-qb", "scratch", cwd=clone)
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "no_upstream" in _issue_codes(rep, "cli")


def test_without_no_upstream_strips_from_matching_branch():
    from pathlib import Path as _P
    from mship.core.repo_state import (
        AuditReport, Issue, RepoAudit, without_no_upstream_on_task_branch,
    )

    r = RepoAudit(
        name="cli", path=_P("/abs/cli"), current_branch="feat/t",
        issues=(
            Issue("no_upstream", "error", "no upstream"),
            Issue("dirty_worktree", "error", "1 file"),
        ),
    )
    report = AuditReport(repos=(r,))
    filtered = without_no_upstream_on_task_branch(report, "feat/t")
    (only,) = filtered.repos
    codes = {i.code for i in only.issues}
    assert "no_upstream" not in codes
    assert "dirty_worktree" in codes  # other issues untouched


def test_without_no_upstream_ignores_other_branches():
    from pathlib import Path as _P
    from mship.core.repo_state import (
        AuditReport, Issue, RepoAudit, without_no_upstream_on_task_branch,
    )

    r = RepoAudit(
        name="cli", path=_P("/abs/cli"), current_branch="main",
        issues=(Issue("no_upstream", "error", "no upstream"),),
    )
    report = AuditReport(repos=(r,))
    filtered = without_no_upstream_on_task_branch(report, "feat/t")
    (only,) = filtered.repos
    assert any(i.code == "no_upstream" for i in only.issues)


def test_audit_extra_worktrees(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    wt = audit_workspace / "cli-wt"
    _sh("git", "worktree", "add", str(wt), "-b", "scratch", cwd=clone)
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "extra_worktrees" in _issue_codes(rep, "cli")


from pathlib import Path as _Path

from mship.core.repo_state import _list_worktree_paths


class _FakeShell:
    def __init__(self, stdout: str, rc: int = 0):
        self._stdout = stdout
        self._rc = rc

    def run(self, cmd: str, cwd, env=None):
        from mship.util.shell import ShellResult
        return ShellResult(returncode=self._rc, stdout=self._stdout, stderr="")


def test_list_worktree_paths_parses_porcelain():
    porcelain = (
        "worktree /abs/main\n"
        "HEAD abc\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /abs/feat-x\n"
        "HEAD def\n"
        "branch refs/heads/feat/x\n"
    )
    shell = _FakeShell(porcelain)
    paths = _list_worktree_paths(shell, _Path("/abs/main"))
    assert [str(p) for p in paths] == ["/abs/main", "/abs/feat-x"]


def test_list_worktree_paths_empty_output():
    shell = _FakeShell("")
    assert _list_worktree_paths(shell, _Path("/abs/main")) == []


def test_list_worktree_paths_returns_resolved_paths(tmp_path):
    # The real resolve call normalizes "." and ".." and symlinks. Simulate by
    # emitting an absolute but unresolved path.
    unresolved = str(tmp_path / "a" / ".." / "a")
    shell = _FakeShell(f"worktree {unresolved}\nHEAD abc\n")
    (tmp_path / "a").mkdir()
    paths = _list_worktree_paths(shell, tmp_path)
    assert paths == [(tmp_path / "a").resolve()]


def test_audit_known_worktree_suppresses_extra_worktrees(audit_workspace):
    """A worktree registered in known_worktree_paths is not counted as extra."""
    import subprocess
    import os
    from mship.core.config import ConfigLoader
    from mship.core.repo_state import audit_repos
    from mship.util.shell import ShellRunner

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    clone = audit_workspace / "cli"
    wt = audit_workspace / "cli-wt"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "scratch"],
        cwd=clone, check=True, capture_output=True, env=env,
    )

    cfg = ConfigLoader.load(audit_workspace / "mothership.yaml")
    shell = ShellRunner()

    # Not excluded → extra_worktrees fires
    rep_open = audit_repos(cfg, shell, names=["cli"])
    codes_open = {i.code for r in rep_open.repos if r.name == "cli" for i in r.issues}
    assert "extra_worktrees" in codes_open

    # Excluded → no extra_worktrees
    known = frozenset({wt.resolve()})
    rep_known = audit_repos(cfg, shell, names=["cli"], known_worktree_paths=known)
    codes_known = {i.code for r in rep_known.repos if r.name == "cli" for i in r.issues}
    assert "extra_worktrees" not in codes_known


def test_audit_foreign_worktree_still_fires(audit_workspace):
    """A worktree NOT in known_worktree_paths still counts as extra."""
    import subprocess
    import os
    from mship.core.config import ConfigLoader
    from mship.core.repo_state import audit_repos
    from mship.util.shell import ShellRunner

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    clone = audit_workspace / "cli"
    wt_known = audit_workspace / "cli-known"
    wt_foreign = audit_workspace / "cli-foreign"
    subprocess.run(
        ["git", "worktree", "add", str(wt_known), "-b", "known-branch"],
        cwd=clone, check=True, capture_output=True, env=env,
    )
    subprocess.run(
        ["git", "worktree", "add", str(wt_foreign), "-b", "foreign-branch"],
        cwd=clone, check=True, capture_output=True, env=env,
    )

    cfg = ConfigLoader.load(audit_workspace / "mothership.yaml")
    shell = ShellRunner()
    known = frozenset({wt_known.resolve()})
    rep = audit_repos(cfg, shell, names=["cli"], known_worktree_paths=known)

    cli_issues = next(r for r in rep.repos if r.name == "cli").issues
    extra = [i for i in cli_issues if i.code == "extra_worktrees"]
    assert len(extra) == 1
    assert "mship prune" in extra[0].message


def test_audit_fetch_failed(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    _sh("git", "remote", "set-url", "origin", "/no/such/path.git", cwd=clone)
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "fetch_failed" in _issue_codes(rep, "cli")


def test_audit_names_filter_unknown_repo(audit_workspace):
    cfg, shell = _load(audit_workspace)
    with pytest.raises(ValueError, match="unknown"):
        audit_repos(cfg, shell, names=["cli", "nope"])


def test_audit_local_only_skips_fetch(audit_workspace):
    """local_only=True must not invoke git fetch."""
    import os, subprocess
    from mship.core.config import ConfigLoader
    from mship.core.repo_state import audit_repos
    from mship.util.shell import ShellRunner

    cfg = ConfigLoader.load(audit_workspace / "mothership.yaml")
    shell = ShellRunner()

    calls: list[str] = []
    real_run = shell.run

    def counting(cmd, cwd, env=None):
        calls.append(cmd)
        return real_run(cmd, cwd, env=env)

    shell.run = counting  # type: ignore[assignment]

    audit_repos(cfg, shell, names=["cli"], local_only=True)
    assert not any(c.startswith("git fetch") for c in calls)


def test_audit_local_only_still_detects_dirty(audit_workspace):
    """Cheap local checks still fire in local_only mode."""
    from mship.core.config import ConfigLoader
    from mship.core.repo_state import audit_repos
    from mship.util.shell import ShellRunner

    (audit_workspace / "cli" / "README.md").write_text("modified\n")

    cfg = ConfigLoader.load(audit_workspace / "mothership.yaml")
    shell = ShellRunner()
    rep = audit_repos(cfg, shell, names=["cli"], local_only=True)
    codes = {i.code for r in rep.repos if r.name == "cli" for i in r.issues}
    assert "dirty_worktree" in codes


def test_audit_local_only_does_not_emit_behind_or_fetch_failed(audit_workspace):
    """Even if tracking is broken, local_only audit never emits fetch-family codes."""
    import os, subprocess
    from mship.core.config import ConfigLoader
    from mship.core.repo_state import audit_repos
    from mship.util.shell import ShellRunner

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    # Break the remote to make fetch fail
    subprocess.run(
        ["git", "-C", str(audit_workspace / "cli"), "remote", "set-url", "origin", "/no/such.git"],
        check=True, capture_output=True, env=env,
    )

    cfg = ConfigLoader.load(audit_workspace / "mothership.yaml")
    shell = ShellRunner()
    rep = audit_repos(cfg, shell, names=["cli"], local_only=True)
    codes = {i.code for r in rep.repos if r.name == "cli" for i in r.issues}
    fetch_family = {"fetch_failed", "behind_remote", "ahead_remote", "diverged", "no_upstream"}
    assert not (codes & fetch_family), f"unexpected fetch-family codes: {codes & fetch_family}"


def test_audit_monorepo_one_fetch_per_root(tmp_path):
    """Two repos sharing a git_root should trigger one fetch and share git-wide issues."""
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    mono = tmp_path / "mono"
    subprocess.run(["git", "clone", str(bare), str(mono)], check=True, capture_output=True)
    for k in ("user.email", "t@t"), ("user.name", "t"):
        subprocess.run(["git", "config", *k], cwd=mono, check=True, capture_output=True)
    (mono / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    (mono / "pkg-a").mkdir()
    (mono / "pkg-b").mkdir()
    (mono / "pkg-a" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    (mono / "pkg-b" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    subprocess.run(["git", "add", "."], cwd=mono, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=mono, check=True, capture_output=True, env=env)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=mono, check=True, capture_output=True)

    (tmp_path / "mothership.yaml").write_text(yaml.safe_dump({
        "workspace": "m",
        "repos": {
            "mono": {"path": "./mono", "type": "service"},
            "pkg_a": {"path": "pkg-a", "type": "library", "git_root": "mono"},
            "pkg_b": {"path": "pkg-b", "type": "library", "git_root": "mono"},
        },
    }))

    # Dirty pkg-a only — branch/fetch state is clean
    (mono / "pkg-a" / "Taskfile.yml").write_text("version: '3'\ntasks: {modified: true}\n")

    cfg, shell = _load(tmp_path)
    # Wrap shell to count fetch calls
    calls: list[str] = []
    real_run = shell.run
    def counting(cmd, cwd, env=None):
        calls.append(cmd)
        return real_run(cmd, cwd, env=env)
    shell.run = counting  # type: ignore[assignment]

    rep = audit_repos(cfg, shell)
    fetch_calls = [c for c in calls if c.startswith("git fetch")]
    assert len(fetch_calls) == 1, f"expected one fetch, got {fetch_calls}"

    pkg_a_codes = _issue_codes(rep, "pkg_a")
    pkg_b_codes = _issue_codes(rep, "pkg_b")
    assert "dirty_worktree" in pkg_a_codes
    assert "dirty_worktree" not in pkg_b_codes


# --- _probe_dirty classification (issue #35) ---

def test_probe_dirty_untracked_only_emits_warn(audit_workspace):
    cfg, shell = _load(audit_workspace)
    (audit_workspace / "cli" / "new.txt").write_text("hi\n")  # untracked
    rep = audit_repos(cfg, shell, names=["cli"])
    cli = next(r for r in rep.repos if r.name == "cli")
    codes = {(i.code, i.severity) for i in cli.issues}
    assert ("dirty_untracked", "warn") in codes
    assert not any(c == "dirty_worktree" for c, _ in codes)
    assert cli.has_errors is False


def test_probe_dirty_modified_tracked_emits_error(audit_workspace):
    cfg, shell = _load(audit_workspace)
    # README.md is a tracked file in the audit_workspace fixture
    (audit_workspace / "cli" / "README.md").write_text("modified content\n")
    rep = audit_repos(cfg, shell, names=["cli"])
    cli = next(r for r in rep.repos if r.name == "cli")
    codes = {(i.code, i.severity) for i in cli.issues}
    assert ("dirty_worktree", "error") in codes
    assert cli.has_errors is True


def test_probe_dirty_mixed_emits_both(audit_workspace):
    cfg, shell = _load(audit_workspace)
    (audit_workspace / "cli" / "README.md").write_text("modified\n")  # tracked-modified
    (audit_workspace / "cli" / "new.txt").write_text("hi\n")          # untracked
    rep = audit_repos(cfg, shell, names=["cli"])
    cli = next(r for r in rep.repos if r.name == "cli")
    codes = {(i.code, i.severity) for i in cli.issues}
    assert ("dirty_worktree", "error") in codes
    assert ("dirty_untracked", "warn") in codes
    assert cli.has_errors is True

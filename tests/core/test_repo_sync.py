import os
import subprocess
from pathlib import Path

from mship.core.config import ConfigLoader
from mship.core.repo_state import audit_repos
from mship.core.repo_sync import sync_repos, SyncResult
from mship.util.shell import ShellRunner


def _sh(*args, cwd):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, env=env)


def _load(ws):
    return ConfigLoader.load(ws / "mothership.yaml"), ShellRunner()


def _result_for(rep, name):
    return next(r for r in rep.results if r.name == name)


def test_sync_clean_repo_up_to_date(audit_workspace):
    cfg, shell = _load(audit_workspace)
    report = audit_repos(cfg, shell)
    out = sync_repos(report, cfg, shell)
    r = _result_for(out, "cli")
    assert r.status == "up_to_date"


def test_sync_behind_repo_fast_forwards(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    (clone / "y.txt").write_text("y")
    _sh("git", "add", "y.txt", cwd=clone)
    _sh("git", "commit", "-qm", "x", cwd=clone)
    _sh("git", "push", "-q", "origin", "main", cwd=clone)
    _sh("git", "reset", "--hard", "HEAD^", cwd=clone)

    report = audit_repos(cfg, shell)
    out = sync_repos(report, cfg, shell)
    r = _result_for(out, "cli")
    assert r.status == "fast_forwarded"
    assert "1" in r.message  # 1 commit


def test_sync_dirty_skipped(audit_workspace):
    cfg, shell = _load(audit_workspace)
    (audit_workspace / "cli" / "x.txt").write_text("x")
    report = audit_repos(cfg, shell)
    out = sync_repos(report, cfg, shell)
    r = _result_for(out, "cli")
    assert r.status == "skipped"
    assert "dirty_worktree" in r.message


def test_sync_diverged_skipped(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    (clone / "a.txt").write_text("a"); _sh("git", "add", "a.txt", cwd=clone); _sh("git", "commit", "-qm", "a", cwd=clone)
    _sh("git", "push", "-q", "origin", "main", cwd=clone)
    _sh("git", "reset", "--hard", "HEAD^", cwd=clone)
    (clone / "b.txt").write_text("b"); _sh("git", "add", "b.txt", cwd=clone); _sh("git", "commit", "-qm", "b", cwd=clone)

    report = audit_repos(cfg, shell)
    out = sync_repos(report, cfg, shell)
    r = _result_for(out, "cli")
    assert r.status == "skipped"
    assert "diverged" in r.message

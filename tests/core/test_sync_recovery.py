"""Integration tests for _try_recover_stale_main.

Uses real `file://` bare-repo origins so git commands exercise actual
index/working-tree logic.
"""
import json
import subprocess
from pathlib import Path

import pytest

from mship.core.repo_sync import _try_recover_stale_main
from mship.core.repo_state import RepoAudit, Issue
from mship.core.config import RepoConfig, WorkspaceConfig
from mship.util.shell import ShellRunner


def _run(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True)


def _setup_workspace(tmp_path: Path, behind: bool = True,
                     dirty_matches_upstream: bool = True,
                     extra_untracked: bool = False,
                     dirty_file_not_in_upstream: bool = False) -> Path:
    """Return the local repo path. Configures an origin ahead of local."""
    origin = tmp_path / "origin.git"
    local = tmp_path / "local"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True)
    _run(local, "config", "user.email", "t@t")
    _run(local, "config", "user.name", "t")

    # Commit A on local, push to origin.
    (local / "a.txt").write_text("A\n")
    _run(local, "add", ".")
    _run(local, "commit", "-qm", "A")
    _run(local, "push", "-qu", "origin", _current_branch(local))

    if behind:
        # Make a commit B on a fresh clone pushed to origin.
        other = tmp_path / "other"
        subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
        _run(other, "config", "user.email", "t@t")
        _run(other, "config", "user.name", "t")
        if dirty_file_not_in_upstream:
            # Don't add the file upstream that's dirty locally.
            (other / "unrelated.txt").write_text("unrelated upstream\n")
            _run(other, "add", ".")
        else:
            (other / "a.txt").write_text("B\n")
            _run(other, "add", ".")
        _run(other, "commit", "-qm", "B")
        _run(other, "push", "-q")
        # Now origin is ahead of local.

    # Dirty local working tree.
    if dirty_matches_upstream and behind and not dirty_file_not_in_upstream:
        (local / "a.txt").write_text("B\n")  # matches upstream
    elif dirty_file_not_in_upstream:
        (local / "a.txt").write_text("user work on a file upstream doesn't touch\n")
    else:
        (local / "a.txt").write_text("user work\n")
    if extra_untracked:
        (local / "user-note.txt").write_text("untracked\n")
    return local


def _current_branch(local: Path) -> str:
    r = _run(local, "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip()


def _make_audit(name: str, path: Path) -> RepoAudit:
    """A RepoAudit whose only issue is dirty_worktree."""
    return RepoAudit(
        name=name, path=path, current_branch=_current_branch(path),
        issues=(Issue("dirty_worktree", "error", "1 modified tracked file"),),
    )


def _make_cfg(name: str, path: Path) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace="t",
        repos={name: RepoConfig(path=path, type="service")},
    )


def test_happy_path_dirty_matches_upstream(tmp_path):
    local = _setup_workspace(tmp_path, behind=True, dirty_matches_upstream=True)
    audit = _make_audit("r", local)
    cfg = _make_cfg("r", local)
    state_dir = tmp_path / ".mothership"

    recovered, msg = _try_recover_stale_main(audit, cfg, ShellRunner(), state_dir)

    assert recovered is True
    assert "recovered" in msg.lower()
    # Working tree is now clean.
    status = _run(local, "status", "--porcelain").stdout.strip()
    assert status == ""
    # Diagnostic file exists.
    diags = list((state_dir / "diagnostics").glob("*.json"))
    assert len(diags) == 1


def test_user_work_preserved_when_hash_mismatches(tmp_path):
    local = _setup_workspace(tmp_path, behind=True, dirty_matches_upstream=False)
    audit = _make_audit("r", local)
    cfg = _make_cfg("r", local)
    state_dir = tmp_path / ".mothership"

    recovered, msg = _try_recover_stale_main(audit, cfg, ShellRunner(), state_dir)

    assert recovered is False
    assert "does not match upstream" in msg.lower() or "real user work" in msg.lower()
    # Original dirty content preserved.
    assert (local / "a.txt").read_text() == "user work\n"
    # Two diagnostics: pre-recovery + real-user-work.
    diags = list((state_dir / "diagnostics").glob("*.json"))
    assert len(diags) == 2


def test_not_behind_origin_is_not_recoverable(tmp_path):
    local = _setup_workspace(tmp_path, behind=False, dirty_matches_upstream=False)
    audit = _make_audit("r", local)
    cfg = _make_cfg("r", local)
    state_dir = tmp_path / ".mothership"

    recovered, msg = _try_recover_stale_main(audit, cfg, ShellRunner(), state_dir)

    assert recovered is False
    assert "not behind" in msg.lower()
    # Original content untouched.
    assert (local / "a.txt").read_text() == "user work\n"
    # One diagnostic (pre-recovery).
    diags = list((state_dir / "diagnostics").glob("*.json"))
    assert len(diags) == 1


def test_untracked_files_block_recovery(tmp_path):
    local = _setup_workspace(tmp_path, behind=True, dirty_matches_upstream=True,
                             extra_untracked=True)
    audit = _make_audit("r", local)
    cfg = _make_cfg("r", local)
    state_dir = tmp_path / ".mothership"

    recovered, msg = _try_recover_stale_main(audit, cfg, ShellRunner(), state_dir)

    assert recovered is False
    assert "untracked" in msg.lower()
    # Tracked dirty file NOT reset (content preserved).
    assert (local / "a.txt").read_text() == "B\n"
    assert (local / "user-note.txt").exists()


def test_dirty_file_not_in_upstream_is_not_recoverable(tmp_path):
    local = _setup_workspace(tmp_path, behind=True,
                             dirty_matches_upstream=False,
                             dirty_file_not_in_upstream=True)
    audit = _make_audit("r", local)
    cfg = _make_cfg("r", local)
    state_dir = tmp_path / ".mothership"

    recovered, msg = _try_recover_stale_main(audit, cfg, ShellRunner(), state_dir)

    assert recovered is False
    assert ("does not match upstream" in msg.lower()
            or "not match" in msg.lower())
    # User content preserved.
    assert "user work" in (local / "a.txt").read_text()


def test_multi_file_all_match_triggers_recovery(tmp_path):
    """Two dirty files, both match upstream → recovery succeeds."""
    origin = tmp_path / "origin.git"
    local = tmp_path / "local"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True)
    _run(local, "config", "user.email", "t@t")
    _run(local, "config", "user.name", "t")

    (local / "a.txt").write_text("A1\n")
    (local / "b.txt").write_text("B1\n")
    _run(local, "add", ".")
    _run(local, "commit", "-qm", "initial")
    branch = _current_branch(local)
    _run(local, "push", "-qu", "origin", branch)

    # Upstream advance.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _run(other, "config", "user.email", "t@t")
    _run(other, "config", "user.name", "t")
    (other / "a.txt").write_text("A2\n")
    (other / "b.txt").write_text("B2\n")
    _run(other, "add", ".")
    _run(other, "commit", "-qm", "advance")
    _run(other, "push", "-q")

    # Dirty both files matching upstream.
    (local / "a.txt").write_text("A2\n")
    (local / "b.txt").write_text("B2\n")

    audit = _make_audit("r", local)
    cfg = _make_cfg("r", local)
    state_dir = tmp_path / ".mothership"

    recovered, msg = _try_recover_stale_main(audit, cfg, ShellRunner(), state_dir)
    assert recovered is True
    # Working tree clean.
    assert _run(local, "status", "--porcelain").stdout.strip() == ""


def test_multi_file_one_mismatches_preserves_all(tmp_path):
    """Two dirty files; first matches, second doesn't.
    Recovery bails before touching anything — both files preserved."""
    origin = tmp_path / "origin.git"
    local = tmp_path / "local"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True)
    _run(local, "config", "user.email", "t@t")
    _run(local, "config", "user.name", "t")
    (local / "a.txt").write_text("A1\n")
    (local / "b.txt").write_text("B1\n")
    _run(local, "add", ".")
    _run(local, "commit", "-qm", "initial")
    branch = _current_branch(local)
    _run(local, "push", "-qu", "origin", branch)

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _run(other, "config", "user.email", "t@t")
    _run(other, "config", "user.name", "t")
    (other / "a.txt").write_text("A2\n")
    (other / "b.txt").write_text("B2\n")
    _run(other, "add", ".")
    _run(other, "commit", "-qm", "advance")
    _run(other, "push", "-q")

    # Dirty: a.txt matches upstream, b.txt has user work.
    (local / "a.txt").write_text("A2\n")
    (local / "b.txt").write_text("user work\n")

    audit = _make_audit("r", local)
    cfg = _make_cfg("r", local)
    state_dir = tmp_path / ".mothership"

    recovered, msg = _try_recover_stale_main(audit, cfg, ShellRunner(), state_dir)
    assert recovered is False
    # Both files preserved — no reset happened.
    assert (local / "a.txt").read_text() == "A2\n"
    assert (local / "b.txt").read_text() == "user work\n"

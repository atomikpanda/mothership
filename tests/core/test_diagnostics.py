import json
import os
import subprocess
from pathlib import Path

import pytest


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def test_capture_snapshot_writes_json_with_required_keys(tmp_path):
    from mship.core.diagnostics import capture_snapshot
    state_dir = tmp_path / ".mothership"
    path = capture_snapshot("sync", "test-reason", state_dir)
    assert path is not None
    assert path.is_file()
    assert path.parent == state_dir / "diagnostics"
    data = json.loads(path.read_text())
    for key in ("captured_at", "command", "reason", "cwd", "mship_version",
                "python_version", "path_env"):
        assert key in data, f"missing key {key}"
    assert data["command"] == "sync"
    assert data["reason"] == "test-reason"


def test_capture_snapshot_filename_is_filesystem_safe(tmp_path):
    """ISO timestamps contain colons on some platforms; must be replaced."""
    from mship.core.diagnostics import capture_snapshot
    state_dir = tmp_path / ".mothership"
    path = capture_snapshot("sync", "a-reason", state_dir)
    assert path is not None
    # No colons in the filename portion.
    assert ":" not in path.name
    # Filename starts with a UTC ISO-like timestamp ending in Z.
    assert path.name.endswith(".json")
    assert "sync" in path.name
    assert "a-reason" in path.name


def test_capture_snapshot_creates_directory(tmp_path):
    """diagnostics/ subdir is created on first call."""
    from mship.core.diagnostics import capture_snapshot
    state_dir = tmp_path / ".mothership"
    assert not (state_dir / "diagnostics").exists()
    capture_snapshot("sync", "r", state_dir)
    assert (state_dir / "diagnostics").is_dir()


def test_capture_snapshot_populates_repos(tmp_path):
    from mship.core.diagnostics import capture_snapshot
    repo = tmp_path / "r"
    _init_git_repo(repo)
    state_dir = tmp_path / ".mothership"
    path = capture_snapshot("sync", "r", state_dir, repos={"r": repo})
    data = json.loads(path.read_text())
    assert "repos" in data
    assert "r" in data["repos"]
    repo_info = data["repos"]["r"]
    for key in ("git_status_porcelain", "head_sha", "head_branch"):
        assert key in repo_info
    assert repo_info["git_status_porcelain"] == ""  # clean repo
    assert len(repo_info["head_sha"]) == 40  # full SHA


def test_capture_snapshot_extra_kwarg_included(tmp_path):
    from mship.core.diagnostics import capture_snapshot
    state_dir = tmp_path / ".mothership"
    path = capture_snapshot("sync", "r", state_dir, extra={"foo": "bar", "n": 42})
    data = json.loads(path.read_text())
    assert data["extra"] == {"foo": "bar", "n": 42}


def test_capture_snapshot_returns_none_on_write_failure(tmp_path, monkeypatch):
    """Write failure never raises; returns None."""
    from mship.core.diagnostics import capture_snapshot
    state_dir = tmp_path / ".mothership"
    # Make the target directory creation fail by making its parent read-only.
    def _raise(*args, **kwargs):
        raise OSError("simulated disk full")
    monkeypatch.setattr(Path, "mkdir", _raise)
    result = capture_snapshot("sync", "r", state_dir)
    assert result is None

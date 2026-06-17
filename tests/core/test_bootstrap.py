import os
import subprocess
from pathlib import Path

from mship.core.bootstrap import bootstrap
from mship.util.shell import ShellRunner


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _make_source_repo(root: Path) -> Path:
    """A real git repo with a Taskfile.yml, usable as a file:// clone source."""
    src = root / "src-lib"
    src.mkdir()
    _git(["init", "-q", "-b", "main"], src)
    (src / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    _git(["add", "."], src)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], src)
    return src


def _workspace(root: Path, url: str, *, member="lib") -> Path:
    ws = root / "ws"
    ws.mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\n"
        "repos:\n"
        f"  {member}:\n"
        f"    path: {member}\n"
        "    type: library\n"
        f"    url: {url}\n"
    )
    (ws / ".mothership").mkdir()
    return ws


def test_bootstrap_clones_missing_member(tmp_path):
    src = _make_source_repo(tmp_path)
    ws = _workspace(tmp_path, f"file://{src}")
    report = bootstrap(ws / "mothership.yaml", ShellRunner(),
                       state_dir=ws / ".mothership")
    statuses = {m.name: m.status for m in report.members}
    assert statuses["lib"] == "cloned"
    assert (ws / "lib" / "Taskfile.yml").exists()
    assert not report.has_errors


def test_bootstrap_skips_existing_dir(tmp_path):
    src = _make_source_repo(tmp_path)
    ws = _workspace(tmp_path, f"file://{src}")
    (ws / "lib").mkdir()  # already present
    report = bootstrap(ws / "mothership.yaml", ShellRunner(),
                       state_dir=ws / ".mothership")
    assert {m.name: m.status for m in report.members}["lib"] == "present"


def test_bootstrap_skips_symlink(tmp_path):
    src = _make_source_repo(tmp_path)
    ws = _workspace(tmp_path, f"file://{src}")
    os.symlink(src, ws / "lib")  # symlink must never be re-pointed
    report = bootstrap(ws / "mothership.yaml", ShellRunner(),
                       state_dir=ws / ".mothership")
    assert {m.name: m.status for m in report.members}["lib"] == "present"
    assert (ws / "lib").is_symlink()  # untouched


def test_bootstrap_unresolvable_url_errors_but_others_proceed(tmp_path):
    src = _make_source_repo(tmp_path)
    ws = tmp_path / "ws2"
    ws.mkdir()
    (ws / ".mothership").mkdir()
    # `bad` has no url and there is no default_remote -> unresolvable.
    # `lib` has a file:// url -> clones fine.
    (ws / "mothership.yaml").write_text(
        "workspace: w\n"
        "repos:\n"
        "  bad:\n    path: bad\n    type: library\n"
        f"  lib:\n    path: lib\n    type: library\n    url: file://{src}\n"
    )
    report = bootstrap(ws / "mothership.yaml", ShellRunner(),
                       state_dir=ws / ".mothership")
    statuses = {m.name: m.status for m in report.members}
    assert statuses["bad"] == "error"
    assert statuses["lib"] == "cloned"
    assert report.has_errors is True
    # doctor is skipped while a member errored
    assert report.doctor_ok is None


def test_bootstrap_repos_filter(tmp_path):
    src = _make_source_repo(tmp_path)
    ws = _workspace(tmp_path, f"file://{src}", member="lib")
    report = bootstrap(ws / "mothership.yaml", ShellRunner(),
                       state_dir=ws / ".mothership", repos=["lib"])
    assert [m.name for m in report.members] == ["lib"]


def test_bootstrap_skips_git_root_repos(tmp_path):
    # A git_root repo is a subdirectory of its parent's checkout — it is
    # materialized when the parent is cloned, never cloned independently.
    src = _make_source_repo(tmp_path)
    ws = tmp_path / "wsg"
    ws.mkdir()
    (ws / ".mothership").mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\n"
        "repos:\n"
        f"  svc:\n    path: svc\n    type: service\n    url: file://{src}\n"
        "  svc_sub:\n    path: sub\n    type: library\n    git_root: svc\n"
    )
    report = bootstrap(ws / "mothership.yaml", ShellRunner(),
                       state_dir=ws / ".mothership")
    names = {m.name for m in report.members}
    assert "svc" in names              # parent is cloned
    assert "svc_sub" not in names      # git_root subdir is skipped, not cloned


def test_bootstrap_unknown_repo_filter_raises(tmp_path):
    src = _make_source_repo(tmp_path)
    ws = _workspace(tmp_path, f"file://{src}", member="lib")
    import pytest
    with pytest.raises(ValueError, match="Unknown repo"):
        bootstrap(ws / "mothership.yaml", ShellRunner(),
                  state_dir=ws / ".mothership", repos=["typo"])

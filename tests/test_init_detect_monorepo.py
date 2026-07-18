"""Integration: `mship init --detect` produces a working single-git monorepo
config (issue #366 finding #4 / spec mship-init-detect-monorepo)."""
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from mship.cli import app
from mship.core.config import ConfigLoader

runner = CliRunner()


def _git(*args, cwd):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _build_single_git_monorepo(tmp_path: Path) -> Path:
    """Real single-git monorepo: root .git; subdirs web/ and infra/ with only
    package.json (no nested .git)."""
    root = tmp_path / "mono"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='mono'\n")
    for sub in ("web", "infra"):
        d = root / sub
        d.mkdir()
        (d / "package.json").write_text("{}")
    _git("init", "-q", ".", cwd=root)
    _git("add", ".", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


def _init_detect(root: Path, monkeypatch) -> Path:
    monkeypatch.chdir(root)
    result = runner.invoke(
        app, ["init", "--name", "mono", "--detect", "--scaffold-taskfiles"]
    )
    assert result.exit_code == 0, result.output
    return root / "mothership.yaml"


def test_detected_monorepo_config_loads_with_require_paths(tmp_path: Path, monkeypatch):
    """ac7 + ac2: the emitted config loads via ConfigLoader.load(require_paths=True)
    — each git_root child resolves to (parent.path / child.path) and finds its
    scaffolded Taskfile.yml — and every emitted path is relative/portable."""
    root = _build_single_git_monorepo(tmp_path)
    cfg_path = _init_detect(root, monkeypatch)

    data = yaml.safe_load(cfg_path.read_text())
    for repo in data["repos"].values():
        assert not str(repo["path"]).startswith("/")
        assert str(root) not in str(repo["path"])

    config = ConfigLoader.load(cfg_path, require_paths=True)   # must not raise
    assert config.repos[root.name].git_root is None
    for sub in ("web", "infra"):
        assert config.repos[sub].git_root == root.name

    # ac2 portability: resolution is anchored on the config's directory, not the
    # process CWD — loading still succeeds from an unrelated cwd.
    monkeypatch.chdir(tmp_path)
    reloaded = ConfigLoader.load(cfg_path, require_paths=True)
    assert set(reloaded.repos) == set(config.repos)


def test_detected_monorepo_doctor_no_not_a_git_repository(tmp_path: Path, monkeypatch):
    """ac4: doctor on a freshly detected single-git monorepo reports NO
    'not a git repository' for the subdir repos — the git check resolves through
    git_root to the root (doctor.py:186-191)."""
    from mship.core.doctor import DoctorChecker
    from mship.util.shell import ShellRunner, ShellResult

    root = _build_single_git_monorepo(tmp_path)
    cfg_path = _init_detect(root, monkeypatch)
    config = ConfigLoader.load(cfg_path, require_paths=True)

    shell = MagicMock(spec=ShellRunner)
    shell.run.return_value = ShellResult(
        returncode=0, stdout="test\nrun\nlint\nsetup\n", stderr=""
    )
    report = DoctorChecker(config, shell).run()

    for name in (root.name, "web", "infra"):
        git_check = next(c for c in report.checks if c.name == f"{name}/git")
        assert git_check.status == "pass", git_check.message
        assert "not a git repository" not in git_check.message

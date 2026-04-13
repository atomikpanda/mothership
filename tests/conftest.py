import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace with repos that have Taskfile.yml files."""
    config = tmp_path / "mothership.yaml"
    config.write_text(
        """\
workspace: test-platform

repos:
  shared:
    path: ./shared
    type: library
    depends_on: []
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
  api-gateway:
    path: ./api-gateway
    type: service
    depends_on: [shared, auth-service]
"""
    )
    for name in ["shared", "auth-service", "api-gateway"]:
        repo_dir = tmp_path / name
        repo_dir.mkdir()
        (repo_dir / "Taskfile.yml").write_text(f"version: '3'\ntasks:\n  test:\n    cmds:\n      - echo {name}\n")
    return tmp_path


@pytest.fixture
def workspace_with_git(workspace: Path) -> Path:
    """Workspace where each repo is a git repo."""
    for name in ["shared", "auth-service", "api-gateway"]:
        repo_dir = workspace / name
        subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.com",
                 "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.com"},
        )
    return workspace


@pytest.fixture
def audit_workspace(tmp_path: Path) -> Path:
    """Workspace with a bare 'origin' and working clone for each of two repos.

    Layout:
        tmp_path/
            origin/{cli,api}.git   # bare
            cli/, api/              # working clones + Taskfile.yml
            mothership.yaml
    """
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def _sh(*args, cwd):
        subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, env=env)

    (tmp_path / "origin").mkdir()
    for name in ("cli", "api"):
        bare = tmp_path / "origin" / f"{name}.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)

        clone = tmp_path / name
        subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)
        _sh("git", "config", "user.email", "t@t", cwd=clone)
        _sh("git", "config", "user.name", "t", cwd=clone)
        (clone / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
        (clone / "README.md").write_text(f"{name}\n")
        _sh("git", "add", ".", cwd=clone)
        _sh("git", "commit", "-qm", "init", cwd=clone)
        _sh("git", "push", "-q", "origin", "main", cwd=clone)

    (tmp_path / "mothership.yaml").write_text(
        "workspace: audit-test\n"
        "repos:\n"
        "  cli:\n    path: ./cli\n    type: service\n"
        "  api:\n    path: ./api\n    type: service\n"
    )
    return tmp_path

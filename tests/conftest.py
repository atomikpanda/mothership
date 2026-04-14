import os
import subprocess
from datetime import datetime, timezone
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


@pytest.fixture
def workspace_monorepo_app(tmp_path: Path):
    """Workspace with a monorepo layout: mono (root), pkg_a and pkg_b (subdirs).

    pkg_a and pkg_b both declare git_root: mono, mimicking a workspace-style
    monorepo where a single git checkout contains multiple packages.
    """
    import os
    import subprocess
    from unittest.mock import MagicMock
    from mship.cli import container
    from mship.util.shell import ShellRunner, ShellResult

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    # Create a bare remote and a working clone (mono)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    mono = tmp_path / "mono"
    subprocess.run(["git", "clone", str(bare), str(mono)],
                   check=True, capture_output=True)
    for k, v in [("user.email", "t@t"), ("user.name", "t")]:
        subprocess.run(["git", "config", k, v], cwd=mono, check=True, capture_output=True)
    (mono / "Taskfile.yml").write_text("version: '3'\ntasks:\n  setup:\n    cmds:\n      - echo ok\n")
    (mono / "pkg-a").mkdir()
    (mono / "pkg-b").mkdir()
    for sub in ["pkg-a", "pkg-b"]:
        (mono / sub / "Taskfile.yml").write_text(
            f"version: '3'\ntasks:\n  setup:\n    cmds:\n      - echo {sub}\n"
        )
    subprocess.run(["git", "add", "."], cwd=mono, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=mono, check=True, capture_output=True, env=env)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=mono, check=True, capture_output=True)

    # Write mothership config
    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(
        "workspace: monorepo-test\n"
        "repos:\n"
        "  mono:\n"
        "    path: ./mono\n"
        "    type: service\n"
        "  pkg_a:\n"
        "    path: ./pkg-a\n"
        "    type: library\n"
        "    git_root: mono\n"
        "  pkg_b:\n"
        "    path: ./pkg-b\n"
        "    type: library\n"
        "    git_root: mono\n"
    )

    state_dir = tmp_path / ".mothership"
    state_dir.mkdir(exist_ok=True)

    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg_path)
    container.state_dir.override(state_dir)

    def _audit_ok_run(cmd, cwd, env=None):
        if "symbolic-ref" in cmd:
            return ShellResult(returncode=0, stdout="main\n", stderr="")
        if "fetch" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/main\n", stderr="")
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "status --porcelain" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "worktree list" in cmd:
            return ShellResult(returncode=0, stdout=f"worktree {mono}\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = _audit_ok_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    yield tmp_path

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.log_manager.reset()
    container.shell.reset_override()


def _sh_switch(*args, cwd, env=None):
    """Shell helper for switch_workspace fixture (sets git author env vars)."""
    e = {**os.environ,
         "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if env is not None:
        e.update(env)
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, env=e)


@pytest.fixture
def switch_workspace(audit_workspace, tmp_path):
    """Extend audit_workspace so 'cli' depends on 'shared'. Both have worktrees for task 't'.

    audit_workspace layout:
        tmp_path/cli/   -- git clone (becomes 'shared' repo in config)
        tmp_path/api/   -- git clone (becomes 'cli' repo in config)

    Config: shared -> ./cli (library), cli -> ./api (service, depends_on shared)
    """
    from mship.core.state import StateManager, Task, WorkspaceState

    # Rewrite config: shared uses ./cli dir, cli uses ./api dir
    cfg_path = audit_workspace / "mothership.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "workspace": "switch-test",
        "repos": {
            "shared": {"path": "./cli", "type": "library"},
            "cli":    {"path": "./api", "type": "service", "depends_on": ["shared"]},
        },
    }))

    # Create a worktree per repo
    shared_wt = audit_workspace / "shared-wt"
    cli_wt = audit_workspace / "cli-wt"
    _sh_switch("git", "worktree", "add", str(shared_wt), "-b", "feat/t",
               cwd=audit_workspace / "cli")
    _sh_switch("git", "worktree", "add", str(cli_wt), "-b", "feat/t",
               cwd=audit_workspace / "api")

    # Seed state
    state_dir = audit_workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    sm = StateManager(state_dir)
    state = WorkspaceState(
        current_task="t",
        tasks={"t": Task(
            slug="t", description="d", phase="dev",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared", "cli"], branch="feat/t",
            worktrees={"shared": shared_wt, "cli": cli_wt},
        )},
    )
    sm.save(state)

    return audit_workspace, shared_wt, cli_wt, sm

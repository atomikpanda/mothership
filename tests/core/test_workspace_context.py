"""WorkspaceContext factory (#472 Task 5): built purely from an explicit
config_path while cwd sits in a DECOY workspace and Path.cwd is poisoned —
the daemon path never discovers."""
import subprocess
from pathlib import Path

import pytest

from mship.core.workspace_context import (
    ContextError,
    WorkspaceContext,
    _resolve_state_dir,
    build_workspace_context,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "HOME": "/tmp", "PATH": "/usr/bin:/bin",
}


def _mk_ws(root: Path, name: str, repos: str | None = None) -> Path:
    ws = root / name
    repo = ws / "app"
    repo.mkdir(parents=True)
    (repo / "Taskfile.yml").write_text("version: '3'\n")
    (ws / "mothership.yaml").write_text(
        repos or f"workspace: {name}\nrepos:\n  app:\n    path: app\n    type: service\n"
    )
    return ws


def _poison_cwd(monkeypatch, decoy: Path):
    monkeypatch.chdir(decoy)
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("Path.cwd read on daemon path"))))


def test_builds_from_explicit_config_path_only(tmp_path: Path, monkeypatch):
    real = _mk_ws(tmp_path, "real")
    decoy = _mk_ws(tmp_path, "decoy")
    _poison_cwd(monkeypatch, decoy)
    ctx = build_workspace_context(real / "mothership.yaml")
    assert isinstance(ctx, WorkspaceContext)
    assert ctx.workspace_root == real
    assert ctx.config.workspace == "real"
    assert ctx.state_dir == real / ".mothership"
    assert ctx.state_manager is not None and ctx.log_manager is not None and ctx.worktree_manager is not None


def test_metarepo_and_monorepo_shapes_build(tmp_path: Path, monkeypatch):
    meta = tmp_path / "meta"
    for r in ("alpha", "beta"):
        (meta / r).mkdir(parents=True)
        (meta / r / "Taskfile.yml").write_text("version: '3'\n")
    (meta / "mothership.yaml").write_text(
        "workspace: meta\nrepos:\n  alpha:\n    path: alpha\n    type: service\n  beta:\n    path: beta\n    type: service\n"
    )
    mono = tmp_path / "mono-ws"
    root_repo = mono / "mono"
    for sub in ("pkg_a",):
        (root_repo / sub).mkdir(parents=True)
        (root_repo / sub / "Taskfile.yml").write_text("version: '3'\n")
    (root_repo / "Taskfile.yml").write_text("version: '3'\n")
    (mono / "mothership.yaml").write_text(
        "workspace: mono-ws\nrepos:\n  mono:\n    path: mono\n    type: service\n  pkg_a:\n    path: pkg_a\n    type: library\n    git_root: mono\n"
    )
    decoy = _mk_ws(tmp_path, "decoy")
    _poison_cwd(monkeypatch, decoy)
    assert build_workspace_context(meta / "mothership.yaml").config.workspace == "meta"
    assert build_workspace_context(mono / "mothership.yaml").config.workspace == "mono-ws"


def test_worktree_config_resolves_shared_state_dir(tmp_path: Path):
    """The tests/cli/test_state_dir_resolution.py:20 case through the moved
    resolver: a linked worktree's config anchors to the MAIN checkout's
    .mothership."""
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=main, check=True, capture_output=True, env=_GIT_ENV)
    (main / "mothership.yaml").write_text("workspace: w\nrepos: {}\n")
    (main / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=main, check=True, capture_output=True, env=_GIT_ENV)
    subprocess.run(["git", "commit", "-m", "c"], cwd=main, check=True, capture_output=True, env=_GIT_ENV)
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", str(wt)], cwd=main, check=True, capture_output=True, env=_GIT_ENV)
    assert _resolve_state_dir(wt / "mothership.yaml") == main / ".mothership"
    assert _resolve_state_dir(main / "mothership.yaml") == main / ".mothership"


def test_missing_config_raises_typed(tmp_path: Path):
    with pytest.raises(ContextError, match="no mothership.yaml"):
        build_workspace_context(tmp_path / "absent" / "mothership.yaml")


def test_invalid_config_raises_typed(tmp_path: Path):
    ws = tmp_path / "bad"
    ws.mkdir()
    (ws / "mothership.yaml").write_text("workspace: [broken\n")
    with pytest.raises(ContextError, match="invalid workspace config"):
        build_workspace_context(ws / "mothership.yaml")


def test_cli_reexport_stands():
    from mship.cli import _resolve_state_dir as reexported

    assert reexported is _resolve_state_dir

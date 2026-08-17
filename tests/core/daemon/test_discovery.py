"""Scanner (#472 Task 3): discovery, prune rule, dedupe, degraded candidates.
Fixture builders make real directories (and real `git worktree add` for the
pollutant cases); all three workspace shapes are covered — single-repo,
monorepo (git_root children), metarepo (sibling repos)."""
import subprocess
from pathlib import Path

from mship.core.daemon.discovery import scan_roots
from mship.core.daemon.registry import DaemonConfig

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "HOME": "/tmp", "PATH": "/usr/bin:/bin",
}


def _git(cwd: Path, *args: str):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**_GIT_ENV})


def _mk_single(root: Path, name: str, ws_name: str | None = None) -> Path:
    ws = root / name
    repo = ws / "app"
    repo.mkdir(parents=True)
    (repo / "Taskfile.yml").write_text("version: '3'\n")
    (ws / "mothership.yaml").write_text(
        f"workspace: {ws_name or name}\nrepos:\n  app:\n    path: app\n    type: service\n"
    )
    return ws


def _mk_metarepo(root: Path, name: str) -> Path:
    ws = root / name
    for r in ("alpha", "beta"):
        d = ws / r
        d.mkdir(parents=True)
        (d / "Taskfile.yml").write_text("version: '3'\n")
    (ws / "mothership.yaml").write_text(
        f"workspace: {name}\nrepos:\n  alpha:\n    path: alpha\n    type: service\n  beta:\n    path: beta\n    type: service\n"
    )
    return ws


def _mk_monorepo(root: Path, name: str) -> Path:
    ws = root / name
    mono = ws / "mono"
    for sub in ("pkg_a", "pkg_b"):
        (mono / sub).mkdir(parents=True)
        (mono / sub / "Taskfile.yml").write_text("version: '3'\n")
    (mono / "Taskfile.yml").write_text("version: '3'\n")
    (ws / "mothership.yaml").write_text(
        f"workspace: {name}\n"
        "repos:\n"
        "  mono:\n    path: mono\n    type: service\n"
        "  pkg_a:\n    path: pkg_a\n    type: library\n    git_root: mono\n"
        "  pkg_b:\n    path: pkg_b\n    type: library\n    git_root: mono\n"
    )
    return ws


def _cfg(*roots: Path, **kw) -> DaemonConfig:
    return DaemonConfig(scan_roots=[str(r) for r in roots], **kw)


def test_two_workspaces_under_one_root(tmp_path: Path):
    _mk_single(tmp_path, "a")
    _mk_metarepo(tmp_path, "b")
    cands = scan_roots(_cfg(tmp_path))
    assert sorted(c.name for c in cands) == ["a", "b"]
    assert all(c.healthy for c in cands)
    meta = next(c for c in cands if c.name == "b")
    assert sorted(r.name for r in meta.repos) == ["alpha", "beta"]
    assert all(r.git_root is None for r in meta.repos)


def test_monorepo_shape_registers_with_git_root_children(tmp_path: Path):
    _mk_monorepo(tmp_path, "m")
    (cand,) = scan_roots(_cfg(tmp_path))
    assert cand.healthy
    by_name = {r.name: r for r in cand.repos}
    assert by_name["pkg_a"].git_root == "mono"
    assert by_name["mono"].git_root is None


def test_overlapping_and_duplicate_roots_dedupe(tmp_path: Path):
    ws = _mk_single(tmp_path / "outer", "a")
    cands = scan_roots(_cfg(tmp_path, tmp_path / "outer", tmp_path / "outer", ws))
    assert len(cands) == 1
    assert cands[0].path == ws.resolve()


def test_root_inside_workspace_walks_up(tmp_path: Path):
    ws = _mk_metarepo(tmp_path, "meta")
    inner_root = ws / "alpha"
    cands = scan_roots(_cfg(inner_root))
    assert len(cands) == 1
    assert cands[0].path == ws.resolve()


def test_nested_marker_only_outermost_registers(tmp_path: Path):
    ws = _mk_single(tmp_path, "outer-ws")
    inner = ws / "sub"
    inner.mkdir()
    (inner / "mothership.yaml").write_text("workspace: inner\nrepos: {}\n")
    cands = scan_roots(_cfg(tmp_path))
    assert [c.name for c in cands] == ["outer-ws"]


def test_worktrees_dir_never_registers(tmp_path: Path):
    ws = _mk_single(tmp_path, "main-ws")
    wt = ws / ".worktrees" / "task-x" / "app"
    wt.mkdir(parents=True)
    (ws / ".worktrees" / "task-x" / "mothership.yaml").write_text("workspace: polluted\nrepos: {}\n")
    cands = scan_roots(_cfg(tmp_path))
    assert [c.name for c in cands] == ["main-ws"]


def test_linked_worktree_outside_worktrees_excluded(tmp_path: Path):
    """A real `git worktree add` to a path outside .worktrees/: .git is a file
    with a gitdir: pointer — never an independent workspace."""
    src = tmp_path / "srcrepo"
    src.mkdir()
    _git(src, "init", "-b", "main")
    (src / "mothership.yaml").write_text("workspace: real\nrepos: {}\n")
    (src / "f.txt").write_text("x")
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "c")
    linked = tmp_path / "elsewhere" / "linked"
    linked.parent.mkdir()
    _git(src, "worktree", "add", str(linked))
    assert (linked / ".git").is_file()  # precondition: gitdir pointer
    cands = scan_roots(_cfg(tmp_path / "elsewhere"))
    assert cands == []


def test_marker_ancestor_excluded(tmp_path: Path):
    """A dir under a .mship-workspace marker (spawned hub layout) is a task
    worktree even if .git detection misses it."""
    from mship.core.workspace_marker import write_marker

    hub = tmp_path / "hub"
    ws_like = hub / "repo"
    ws_like.mkdir(parents=True)
    (ws_like / "mothership.yaml").write_text("workspace: inherited\nrepos: {}\n")
    real = tmp_path / "real-workspace"
    real.mkdir()
    (real / "mothership.yaml").write_text("workspace: real\nrepos: {}\n")
    write_marker(hub, real)
    assert scan_roots(_cfg(hub)) == []


def test_template_yaml_degrades(tmp_path: Path):
    ws = tmp_path / "examples"
    ws.mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: example\nrepos:\n  app:\n    path: does-not-exist\n    type: service\n"
    )
    (cand,) = scan_roots(_cfg(tmp_path))
    assert not cand.healthy
    assert "template" in cand.detail


def test_broken_yaml_degrades_sibling_still_found(tmp_path: Path):
    _mk_single(tmp_path, "good")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "mothership.yaml").write_text("workspace: [unclosed\n")
    cands = scan_roots(_cfg(tmp_path))
    states = {c.name or c.path.name: c.healthy for c in cands}
    assert states.pop("good") is True
    ((_, bad_healthy),) = states.items()
    assert bad_healthy is False


def test_missing_workspace_key_degrades(tmp_path: Path):
    bad = tmp_path / "noname"
    bad.mkdir()
    (bad / "mothership.yaml").write_text("repos: {}\n")
    (cand,) = scan_roots(_cfg(tmp_path))
    assert not cand.healthy
    assert "invalid" in cand.detail


def test_symlinked_dir_not_followed(tmp_path: Path):
    real = tmp_path / "outside"
    _mk_single(real, "hidden")
    scanned = tmp_path / "scanned"
    scanned.mkdir()
    (scanned / "link").symlink_to(real / "hidden")
    assert scan_roots(_cfg(scanned)) == []


def test_max_depth_respected(tmp_path: Path):
    deep = tmp_path / "a" / "b" / "c"
    _mk_single(deep, "deep-ws")
    assert scan_roots(_cfg(tmp_path, max_depth=2)) == []
    assert len(scan_roots(_cfg(tmp_path, max_depth=6))) == 1


def test_same_basename_both_found(tmp_path: Path):
    _mk_single(tmp_path / "x", "proj", ws_name="proj")
    _mk_single(tmp_path / "y", "proj", ws_name="proj")
    cands = scan_roots(_cfg(tmp_path))
    assert len(cands) == 2
    assert {c.path for c in cands} == {(tmp_path / "x" / "proj").resolve(), (tmp_path / "y" / "proj").resolve()}


def test_empty_roots_scan_nothing(tmp_path: Path):
    assert scan_roots(DaemonConfig()) == []


def test_ignore_globs(tmp_path: Path):
    _mk_single(tmp_path / "archive", "old-ws")
    _mk_single(tmp_path / "live", "new-ws")
    cands = scan_roots(_cfg(tmp_path, ignore_globs=["archive"]))
    assert [c.name for c in cands] == ["new-ws"]


def test_venv_runtime_and_runner_recorded(tmp_path: Path):
    ws = _mk_single(tmp_path, "rt")
    venv_bin = ws / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").touch()
    (ws / "mothership.yaml").write_text(
        "workspace: rt\nrepos:\n  app:\n    path: app\n    type: service\nrunner:\n  enabled: true\n  concurrency: 2\n"
    )
    (cand,) = scan_roots(_cfg(tmp_path))
    assert cand.runtime.venv_path == str(ws / ".venv")
    assert cand.runtime.interpreter == str(venv_bin / "python")
    assert cand.runner == {"enabled": True, "concurrency": 2}

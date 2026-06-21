"""Tests for `mship capture` CLI."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


def _bootstrap(tmp_path: Path, platforms: list[str]):
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    wt = tmp_path / "wt"; wt.mkdir()
    app_dir = tmp_path / "app"; app_dir.mkdir()
    (app_dir / "Taskfile.yml").write_text("version: '3'\ntasks:\n  capture:\n    cmds:\n      - echo ok\n")
    cfg = tmp_path / "mothership.yaml"
    plat = "[" + ", ".join(platforms) + "]" if platforms else "[]"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
        f"    capture:\n      platforms: {plat}\n"
    )
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["app"], worktrees={"app": str(wt)}, branch="feat/t",
        base_branch="main", active_repo="app",
    )
    StateManager(state_dir).save(WorkspaceState(tasks={"t": task}))
    return cfg, state_dir, wt


def _override(cfg, state_dir, shell):
    container.config.reset(); container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    container.shell.override(shell)


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override(); container.config.reset()
    container.state_manager.reset_override(); container.state_manager.reset()
    container.shell.reset_override()


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode; self.stdout = stdout; self.stderr = stderr


class _FakeShell:
    def __init__(self, returncode=0, stderr="", write_image=True):
        self.returncode = returncode; self.stderr = stderr; self.write_image = write_image
        self.calls = []   # env dicts, one per run_task
        self.cwds = []    # cwd Paths, one per run_task

    def run_task(self, task_name, actual_task_name, cwd, env_runner=None, env=None):
        self.calls.append(env)
        self.cwds.append(cwd)
        out = Path(env["MSHIP_CAPTURE_DIR"]); out.mkdir(parents=True, exist_ok=True)
        if self.write_image:
            (out / "screen.png").write_bytes(b"PNGDATA")
        return _FakeResult(returncode=self.returncode, stderr=self.stderr)


def _bootstrap_no_task(tmp_path: Path, repos: dict[str, list[str]]):
    """Workspace with NO active task. `repos` maps repo name -> capture platforms."""
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    lines = ["workspace: t", "repos:"]
    for name, platforms in repos.items():
        repo_dir = tmp_path / name; repo_dir.mkdir()
        (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks:\n  capture:\n    cmds:\n      - echo ok\n")
        plat = "[" + ", ".join(platforms) + "]"
        lines += [f"  {name}:", f"    path: ./{name}", "    type: service",
                  "    capture:", f"      platforms: {plat}"]
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("\n".join(lines) + "\n")
    StateManager(state_dir).save(WorkspaceState(tasks={}))
    return cfg, state_dir


def test_capture_no_task_adhoc_uses_main_checkout(tmp_path):
    cfg, state_dir = _bootstrap_no_task(tmp_path, {"app": ["android"]})
    shell = _FakeShell()
    _override(cfg, state_dir, shell)
    try:
        result = runner.invoke(app, ["capture", "--repo", "app"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["platform"] == "android"
        assert payload["resolved_task"] is None
        assert payload["artifacts"][0]["kind"] == "image"
        # ran in the repo's main checkout, not a worktree
        assert shell.cwds[0] == (tmp_path / "app").resolve()
        # output filed under the _adhoc bucket, not a task slug
        assert "_adhoc" in payload["artifacts"][0]["path"]
    finally:
        _reset()


def test_capture_no_task_requires_repo_when_multiple(tmp_path):
    cfg, state_dir = _bootstrap_no_task(tmp_path, {"app": ["android"], "web": ["browser"]})
    _override(cfg, state_dir, _FakeShell())
    try:
        result = runner.invoke(app, ["capture"])
        assert result.exit_code != 0
        assert "no active task" in result.output
        assert "--repo" in result.output
    finally:
        _reset()


def test_capture_no_task_with_repo_flag_picks_it(tmp_path):
    cfg, state_dir = _bootstrap_no_task(tmp_path, {"app": ["android"], "web": ["browser"]})
    shell = _FakeShell()
    _override(cfg, state_dir, shell)
    try:
        result = runner.invoke(app, ["capture", "--repo", "web", "--platform", "browser"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["repo"] == "web"
        assert payload["platform"] == "browser"
        assert payload["resolved_task"] is None
    finally:
        _reset()


def test_capture_ambiguous_task_still_errors(tmp_path):
    # Two active tasks, no anchor -> ambiguous; must error, NOT fall back to adhoc.
    cfg, state_dir = _bootstrap_no_task(tmp_path, {"app": ["android"]})
    wt1 = tmp_path / "wt1"; wt1.mkdir()
    wt2 = tmp_path / "wt2"; wt2.mkdir()
    now = datetime.now(timezone.utc)
    StateManager(state_dir).save(WorkspaceState(tasks={
        "a": Task(slug="a", description="d", phase="dev", created_at=now,
                  affected_repos=["app"], worktrees={"app": str(wt1)}, branch="feat/a", base_branch="main"),
        "b": Task(slug="b", description="d", phase="dev", created_at=now,
                  affected_repos=["app"], worktrees={"app": str(wt2)}, branch="feat/b", base_branch="main"),
    }))
    _override(cfg, state_dir, _FakeShell())
    try:
        result = runner.invoke(app, ["capture", "--repo", "app", "--platform", "android"])
        assert result.exit_code != 0
        assert "a" in result.output and "b" in result.output
    finally:
        _reset()


def test_capture_single_platform_implicit(tmp_path):
    cfg, state_dir, wt = _bootstrap(tmp_path, ["android"])
    shell = _FakeShell()
    _override(cfg, state_dir, shell)
    try:
        result = runner.invoke(app, ["capture", "--task", "t", "--repo", "app"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["platform"] == "android"
        assert payload["artifacts"][0]["kind"] == "image"
        assert shell.calls[0]["MSHIP_CAPTURE_PLATFORM"] == "android"
    finally:
        _reset()


def test_capture_requires_platform_when_multiple(tmp_path):
    cfg, state_dir, wt = _bootstrap(tmp_path, ["android", "ios"])
    shell = _FakeShell()
    _override(cfg, state_dir, shell)
    try:
        result = runner.invoke(app, ["capture", "--task", "t", "--repo", "app"])
        assert result.exit_code != 0
        assert "--platform is required" in result.output
        assert "android" in result.output and "ios" in result.output
    finally:
        _reset()


def test_capture_explicit_platform_and_out(tmp_path):
    cfg, state_dir, wt = _bootstrap(tmp_path, ["android", "ios"])
    shell = _FakeShell()
    out = tmp_path / "shots"
    _override(cfg, state_dir, shell)
    try:
        result = runner.invoke(
            app, ["capture", "--task", "t", "--repo", "app", "--platform", "ios", "--out", str(out)]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["platform"] == "ios"
        assert payload["artifacts"][0]["path"] == str(out / "screen.png")
    finally:
        _reset()


def test_capture_unknown_platform_errors(tmp_path):
    cfg, state_dir, wt = _bootstrap(tmp_path, ["android", "ios"])
    _override(cfg, state_dir, _FakeShell())
    try:
        result = runner.invoke(app, ["capture", "--task", "t", "--repo", "app", "--platform", "web"])
        assert result.exit_code != 0
        assert "unknown platform" in result.output
    finally:
        _reset()


def test_capture_target_failure_surfaces_stderr(tmp_path):
    cfg, state_dir, wt = _bootstrap(tmp_path, ["android"])
    _override(cfg, state_dir, _FakeShell(returncode=1, stderr="adb: device offline", write_image=False))
    try:
        result = runner.invoke(app, ["capture", "--task", "t", "--repo", "app"])
        assert result.exit_code != 0
        assert "adb: device offline" in result.output
    finally:
        _reset()


def test_capture_invalid_kind_errors(tmp_path):
    cfg, state_dir, wt = _bootstrap(tmp_path, ["android"])
    _override(cfg, state_dir, _FakeShell())
    try:
        result = runner.invoke(app, ["capture", "--task", "t", "--repo", "app", "--kind", "video"])
        assert result.exit_code == 2
        assert "unknown kind" in result.output
    finally:
        _reset()

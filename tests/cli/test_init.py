from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


@pytest.fixture
def init_workspace(tmp_path: Path) -> Path:
    for name in ["shared", "auth-service"]:
        d = tmp_path / name
        d.mkdir()
        (d / ".git").mkdir()
        (d / "Taskfile.yml").write_text("version: '3'")
    return tmp_path


def test_init_non_interactive_with_cwd(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    result = runner.invoke(app, [
        "init",
        "--name", "test-platform",
        "--repo", "./shared:library",
        "--repo", "./auth-service:service:shared",
    ])
    assert result.exit_code == 0, result.output
    config_path = init_workspace / "mothership.yaml"
    assert config_path.exists()
    with open(config_path) as f:
        data = yaml.safe_load(f)
    assert data["workspace"] == "test-platform"
    assert "shared" in data["repos"]
    assert data["repos"]["shared"]["type"] == "library"
    assert data["repos"]["auth-service"]["type"] == "service"
    assert data["repos"]["auth-service"]["depends_on"] == ["shared"]


def test_init_detect(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    result = runner.invoke(app, [
        "init",
        "--name", "test-platform",
        "--detect",
    ])
    assert result.exit_code == 0, result.output
    config_path = init_workspace / "mothership.yaml"
    assert config_path.exists()
    with open(config_path) as f:
        data = yaml.safe_load(f)
    assert "shared" in data["repos"]
    assert "auth-service" in data["repos"]


def test_init_detect_emits_git_root_for_single_git_monorepo(tmp_path: Path, monkeypatch):
    """ac1/ac2: `init --detect` on a single-git monorepo emits the root as
    `path: .` (no git_root) and each markerless subdir as a git_root child with
    a relative path."""
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='root'\n")
    for sub in ("web", "infra"):
        d = tmp_path / sub
        d.mkdir()
        (d / "package.json").write_text("{}")

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--name", "mono", "--detect"])
    assert result.exit_code == 0, result.output

    data = yaml.safe_load((tmp_path / "mothership.yaml").read_text())
    root_name = tmp_path.name
    assert data["repos"][root_name]["path"] == "."
    assert "git_root" not in data["repos"][root_name]
    for sub in ("web", "infra"):
        assert data["repos"][sub]["path"] == sub
        assert data["repos"][sub]["git_root"] == root_name
    for repo in data["repos"].values():          # ac2
        assert not str(repo["path"]).startswith("/")


def test_init_already_exists(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    (init_workspace / "mothership.yaml").write_text("workspace: existing")
    result = runner.invoke(app, [
        "init",
        "--name", "test",
        "--repo", "./shared:library",
    ])
    assert result.exit_code != 0 or "already exists" in result.output.lower()


def test_init_force_overwrite(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    (init_workspace / "mothership.yaml").write_text("workspace: existing")
    result = runner.invoke(app, [
        "init",
        "--name", "test",
        "--repo", "./shared:library",
        "--force",
    ])
    assert result.exit_code == 0, result.output


def test_init_env_runner(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    result = runner.invoke(app, [
        "init",
        "--name", "test",
        "--repo", "./shared:library",
        "--env-runner", "dotenvx run --",
    ])
    assert result.exit_code == 0, result.output
    with open(init_workspace / "mothership.yaml") as f:
        data = yaml.safe_load(f)
    assert data["env_runner"] == "dotenvx run --"


def test_init_scaffold_taskfiles(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    no_taskfile = init_workspace / "new-repo"
    no_taskfile.mkdir()
    (no_taskfile / ".git").mkdir()
    result = runner.invoke(app, [
        "init",
        "--name", "test",
        "--repo", "./new-repo:service",
        "--scaffold-taskfiles",
    ])
    assert result.exit_code == 0, result.output
    assert (no_taskfile / "Taskfile.yml").exists()


def test_init_no_args_no_tty(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    result = runner.invoke(app, ["init"])
    assert result.exit_code != 0


def test_install_hooks_output_per_hook_per_root(tmp_path: Path, monkeypatch):
    """Test that --install-hooks outputs per-hook per-root outcome lines."""
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  only:\n"
        "    path: .\n"
        "    type: service\n"
    )
    (tmp_path / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    (tmp_path / ".git" / "hooks").mkdir(parents=True)

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(tmp_path / ".mothership")
    try:
        result = runner.invoke(app, ["init", "--install-hooks"])
        assert result.exit_code == 0, result.output
        for hook_name in ("pre-commit", "post-commit", "post-checkout"):
            assert hook_name in result.output
        assert "installed" in result.output
        assert str(tmp_path / ".git" / "hooks") in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_install_hooks_prepush_and_session_hook(tmp_path: Path, monkeypatch):
    """--install-hooks installs pre-push and writes the SessionStart hook to .claude/settings.json."""
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  only:\n"
        "    path: .\n"
        "    type: service\n"
    )
    (tmp_path / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    (tmp_path / ".git" / "hooks").mkdir(parents=True)

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(tmp_path / ".mothership")
    try:
        result = runner.invoke(app, ["init", "--install-hooks"])
        assert result.exit_code == 0, result.output

        # pre-push hook file must exist
        assert (tmp_path / ".git" / "hooks" / "pre-push").exists(), (
            f"pre-push hook not created. Output: {result.output}"
        )
        # pre-push must appear in output
        assert "pre-push" in result.output

        # SessionStart hook must be written to .claude/settings.json
        settings_path = tmp_path / ".claude" / "settings.json"
        assert settings_path.exists(), f"settings.json not created. Output: {result.output}"
        import json
        data = json.loads(settings_path.read_text())
        session_hooks = data.get("hooks", {}).get("SessionStart", [])
        assert any(
            h.get("command") == "mship _session-context"
            for entry in session_hooks if isinstance(entry, dict)
            for h in (entry.get("hooks") or []) if isinstance(h, dict)
        ), f"mship _session-context not found in SessionStart hooks. settings.json: {data}"

        # output must mention SessionStart
        assert "SessionStart" in result.output
        assert "installed" in result.output

        # Second run: must report 'up to date' for session hook
        result2 = runner.invoke(app, ["init", "--install-hooks"])
        assert result2.exit_code == 0, result2.output
        assert "up to date" in result2.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_install_hooks_refreshed_vs_up_to_date_labels(tmp_path: Path, monkeypatch):
    """Test that second run shows 'refreshed' for modified hooks and 'up to date' for others."""
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  only:\n"
        "    path: .\n"
        "    type: service\n"
    )
    (tmp_path / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    (tmp_path / ".git" / "hooks").mkdir(parents=True)

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(tmp_path / ".mothership")
    try:
        # First run: fresh install
        result1 = runner.invoke(app, ["init", "--install-hooks"])
        assert result1.exit_code == 0, result1.output
        # Stale-ify the post-commit hook
        post_commit = tmp_path / ".git" / "hooks" / "post-commit"
        assert post_commit.exists(), f"post-commit hook not created by first run. Output: {result1.output}"
        post_commit.write_text(post_commit.read_text().replace("_journal-commit", "_log-commit"))
        # Second run
        result = runner.invoke(app, ["init", "--install-hooks"])
        assert result.exit_code == 0, result.output
        assert "post-commit" in result.output
        assert "refreshed" in result.output
        assert "up to date" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_interactive_wizard_emits_git_root_for_single_git_monorepo(tmp_path: Path, monkeypatch):
    """The interactive wizard (plain `mship init` in a TTY) emits the SAME
    relative-path + git_root monorepo config as `--detect` on a single-git
    monorepo — closes the interactive-vs-detect divergence (issue #366 #4)."""
    import subprocess

    import InquirerPy.inquirer  # noqa: F401  (import so the patch target exists)
    from mship.cli.init import _run_interactive
    from mship.cli.output import Output
    from mship.core.init import WorkspaceInitializer

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='root'\n")
    for sub in ("web", "infra"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "package.json").write_text("{}")

    class _Prompt:
        def __init__(self, val):
            self._val = val

        def execute(self):
            return self._val

    class _FakeInquirer:
        # workspace name, then the manual-add loop (blank == stop)
        def text(self, message="", default="", **kw):
            return _Prompt("mono") if "Workspace name" in message else _Prompt("")

        # select-repos (take all) vs depends_on (none)
        def checkbox(self, message="", choices=None, **kw):
            if "Select repos" in message:
                return _Prompt([c["value"] for c in (choices or [])])
            return _Prompt([])

        # per-repo type vs env_runner
        def select(self, message="", choices=None, default=None, **kw):
            return _Prompt("service") if "type is" in message else _Prompt(None)

        # taskfile scaffolding prompt
        def confirm(self, message="", default=True, **kw):
            return _Prompt(False)

    monkeypatch.setattr("InquirerPy.inquirer", _FakeInquirer())

    _run_interactive(
        WorkspaceInitializer(), Output(), tmp_path,
        tmp_path / "mothership.yaml", None, False,
    )

    data = yaml.safe_load((tmp_path / "mothership.yaml").read_text())
    root_name = tmp_path.name
    assert data["repos"][root_name]["path"] == "."
    assert "git_root" not in data["repos"][root_name]
    for sub in ("web", "infra"):
        assert data["repos"][sub]["path"] == sub
        assert data["repos"][sub]["git_root"] == root_name

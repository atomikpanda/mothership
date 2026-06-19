"""Tests for hidden _check-commit / _post-checkout / _journal-commit commands."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container


runner = CliRunner()


def test_get_container_required_false_returns_none_when_no_workspace(tmp_path, monkeypatch, capsys):
    """Outside any workspace, get_container(required=False) must be silent.
    See #86."""
    from mship.cli import get_container
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    container.config_path.reset_override()
    container.state_dir.reset_override()
    result = get_container(required=False)
    captured = capsys.readouterr()
    assert result is None
    assert captured.err == ""  # no "No mothership.yaml found" noise
    assert captured.out == ""


def test_get_container_required_true_still_errors_loudly(tmp_path, monkeypatch, capsys):
    """Regression: default behavior unchanged — prints + raises."""
    import typer
    from mship.cli import get_container
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    container.config_path.reset_override()
    container.state_dir.reset_override()
    with pytest.raises(typer.Exit) as exc:
        get_container()  # required=True by default
    captured = capsys.readouterr()
    assert exc.value.exit_code == 1
    assert "No mothership.yaml" in captured.err


def test_check_commit_silent_outside_workspace(tmp_path, monkeypatch):
    """_check-commit in a dir with no workspace ancestor exits 0 silently.
    See #86."""
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    container.config_path.reset_override()
    container.state_dir.reset_override()
    result = runner.invoke(app, ["_check-commit", str(tmp_path)])
    assert result.exit_code == 0
    assert "No mothership.yaml" not in (result.output or "")


def test_journal_commit_silent_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    container.config_path.reset_override()
    container.state_dir.reset_override()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["_journal-commit"])
    assert result.exit_code == 0
    assert "No mothership.yaml" not in (result.output or "")


def test_check_commit_refuses_passive_worktree(tmp_path, monkeypatch):
    """A commit attempted in a registered-but-passive worktree is rejected."""
    from datetime import datetime, timezone
    from mship.core.state import StateManager, Task, WorkspaceState
    from typer.testing import CliRunner
    from mship.cli import app, container

    # Workspace skeleton
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n  shared:\n    path: ./shared\n    type: library\n"
    )
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    passive_wt = tmp_path / ".worktrees" / "x" / "shared"
    passive_wt.mkdir(parents=True)
    StateManager(state_dir).save(WorkspaceState(tasks={
        "x": Task(
            slug="x", description="x", phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/x",
            worktrees={"shared": passive_wt},
            passive_repos={"shared"},
        )
    }))

    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(state_dir)
    monkeypatch.chdir(passive_wt)
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["_check-commit", str(passive_wt)])
        assert result.exit_code == 1
        # CliRunner mixes stdout and stderr by default; check `output`.
        out = (result.output or "")
        assert "passive worktree" in out.lower()
        assert "shared" in out
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


# ---------------------------------------------------------------------------
# Helpers for _check-push / _session-context tests
# ---------------------------------------------------------------------------

def _setup(tmp_path, monkeypatch, tasks_yaml="tasks: {}\n"):
    ws = tmp_path
    (ws / "mothership.yaml").write_text(
        "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
    )
    (ws / "lib").mkdir()
    (ws / "lib" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    sd = ws / ".mothership"
    sd.mkdir()
    (sd / "state.yaml").write_text(tasks_yaml)
    container.config_path.override(ws / "mothership.yaml")
    container.state_dir.override(sd)
    container.config.reset()
    container.state_manager.reset()
    monkeypatch.chdir(ws)


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def _push(branch, sha="a" * 40):
    return f"refs/heads/{branch} {sha} refs/heads/{branch} {'0'*40}\n"


_TASK_YAML = (
    "tasks:\n  known:\n    slug: known\n    description: d\n    phase: dev\n"
    "    created_at: 2026-01-01T00:00:00+00:00\n    affected_repos: [lib]\n"
    "    branch: feat/known\n"
)


def test_check_push_rejects_unregistered_feat_branch(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        r = runner.invoke(app, ["_check-push"], input=_push("feat/hand-rolled"))
        assert r.exit_code == 1
    finally:
        _reset()


def test_check_push_allows_registered_task_branch(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, tasks_yaml=_TASK_YAML)
    try:
        r = runner.invoke(app, ["_check-push"], input=_push("feat/known"))
        assert r.exit_code == 0
    finally:
        _reset()


def test_check_push_allows_non_pattern_branch(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        r = runner.invoke(app, ["_check-push"], input=_push("main"))
        assert r.exit_code == 0
    finally:
        _reset()


def test_check_push_allows_delete(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        r = runner.invoke(app, ["_check-push"], input=_push("feat/x", sha="0" * 40))
        assert r.exit_code == 0
    finally:
        _reset()


def test_check_push_bypass_allows_and_logs(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MSHIP_BYPASS_GATE", "intentional")
    try:
        r = runner.invoke(app, ["_check-push"], input=_push("feat/x"))
        assert r.exit_code == 0
        log = tmp_path / ".mothership" / "bypass-log.jsonl"
        assert log.exists() and "intentional" in log.read_text()
    finally:
        _reset()


def test_check_push_no_prefix_pattern_allows_all(tmp_path, monkeypatch):
    ws = tmp_path
    (ws / "mothership.yaml").write_text(
        "workspace: w\nbranch_pattern: '{slug}'\n"
        "repos:\n  lib:\n    path: lib\n    type: library\n"
    )
    (ws / "lib").mkdir(); (ws / "lib" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    sd = ws / ".mothership"; sd.mkdir(); (sd / "state.yaml").write_text("tasks: {}\n")
    container.config_path.override(ws / "mothership.yaml"); container.state_dir.override(sd)
    container.config.reset(); container.state_manager.reset()
    monkeypatch.chdir(ws)
    try:
        r = runner.invoke(app, ["_check-push"], input=_push("anything"))
        assert r.exit_code == 0
    finally:
        _reset()


def test_check_push_dedupes_repeated_branch(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MSHIP_BYPASS_GATE", "1")
    try:
        r = runner.invoke(app, ["_check-push"], input=_push("feat/x") + _push("feat/x"))
        assert r.exit_code == 0
        lines = (tmp_path / ".mothership" / "bypass-log.jsonl").read_text().splitlines()
        assert sum(1 for ln in lines if "feat/x" in ln) == 1
    finally:
        _reset()


def test_session_context_prints_notice_when_no_task(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        r = runner.invoke(app, ["_session-context"])
        assert r.exit_code == 0
        assert "no active task" in r.stdout.lower()
    finally:
        _reset()


def test_session_context_silent_with_task(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, tasks_yaml=_TASK_YAML)
    try:
        r = runner.invoke(app, ["_session-context"])
        assert r.exit_code == 0
        assert r.stdout.strip() == ""
    finally:
        _reset()

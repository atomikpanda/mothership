from datetime import datetime, timezone
from pathlib import Path

import pytest
import typer

from mship.core.state import Task, WorkspaceState
from mship.cli._resolve import resolve_or_exit


def _task(slug: str) -> Task:
    return Task(
        slug=slug, description="d", phase="plan",
        created_at=datetime(2026, 4, 16, tzinfo=timezone.utc),
        affected_repos=[], branch=f"feat/{slug}",
    )


def test_returns_task_when_resolved(monkeypatch, tmp_path):
    state = WorkspaceState(tasks={"A": _task("A")})
    monkeypatch.setenv("MSHIP_TASK", "A")
    monkeypatch.chdir(tmp_path)
    t = resolve_or_exit(state, cli_task=None)
    assert t.slug == "A"


def test_no_active_exits_nonzero(monkeypatch, tmp_path, capsys):
    state = WorkspaceState(tasks={})
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(typer.Exit) as exc:
        resolve_or_exit(state, cli_task=None)
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "no active task" in err.lower()


def test_unknown_task_exits_nonzero_lists_known(monkeypatch, tmp_path, capsys):
    state = WorkspaceState(tasks={"A": _task("A")})
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(typer.Exit):
        resolve_or_exit(state, cli_task="nope")
    err = capsys.readouterr().err
    assert "nope" in err
    assert "A" in err


def test_ambiguous_exits_nonzero_lists_active(monkeypatch, tmp_path, capsys):
    state = WorkspaceState(tasks={"A": _task("A"), "B": _task("B")})
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(typer.Exit):
        resolve_or_exit(state, cli_task=None)
    err = capsys.readouterr().err
    assert "A" in err and "B" in err
    assert "--task" in err or "MSHIP_TASK" in err

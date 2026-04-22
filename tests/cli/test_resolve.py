"""Tests for resolve_for_command: breadcrumb + ambiguity rendering."""
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output
from mship.core.state import Task, WorkspaceState


def _task(slug: str, worktree: Path | None = None) -> Task:
    return Task(
        slug=slug,
        description=f"d {slug}",
        phase="plan",
        created_at=datetime(2026, 4, 22, tzinfo=timezone.utc),
        affected_repos=["r"] if worktree else [],
        branch=f"feat/{slug}",
        worktrees={"r": worktree} if worktree else {},
    )


class _TTYStream:
    def __init__(self):
        self._buf = io.StringIO()
    def write(self, s):
        self._buf.write(s)
    def flush(self):
        pass
    def isatty(self):
        return True
    def getvalue(self):
        return self._buf.getvalue()


class _NonTTYStream(_TTYStream):
    def isatty(self):
        return False


def _tty_output():
    out, err = _TTYStream(), _TTYStream()
    return Output(stream=out, err_stream=err), err


def _nontty_output():
    out, err = _NonTTYStream(), _NonTTYStream()
    return Output(stream=out, err_stream=err), out, err


def test_breadcrumb_printed_on_tty(tmp_path: Path, monkeypatch):
    wt = tmp_path / "wt"; wt.mkdir()
    state = WorkspaceState(tasks={"A": _task("A", wt)})
    monkeypatch.chdir(wt)
    output, err = _tty_output()
    result = resolve_for_command("finish", state, cli_task=None, output=output)
    assert result.task.slug == "A"
    assert result.source == "cwd"
    body = err.getvalue()
    assert "→ task: A" in body
    assert "cwd" in body.lower()


def test_breadcrumb_source_is_cli_flag(tmp_path: Path, monkeypatch):
    state = WorkspaceState(tasks={"A": _task("A")})
    monkeypatch.chdir(tmp_path)
    output, err = _tty_output()
    result = resolve_for_command("finish", state, cli_task="A", output=output)
    assert result.source == "--task"
    assert "--task" in err.getvalue()


def test_no_breadcrumb_when_non_tty(tmp_path: Path, monkeypatch):
    wt = tmp_path / "wt"; wt.mkdir()
    state = WorkspaceState(tasks={"A": _task("A", wt)})
    monkeypatch.chdir(wt)
    output, out, err = _nontty_output()
    result = resolve_for_command("finish", state, cli_task=None, output=output)
    assert result.source == "cwd"
    assert out.getvalue() == ""
    assert err.getvalue() == ""


def test_ambiguity_lists_candidates_on_tty(tmp_path: Path, monkeypatch):
    """No-anchor + 2 tasks raises typer.Exit(1) and lists --task candidates."""
    wt_a = tmp_path / "a"; wt_a.mkdir()
    wt_b = tmp_path / "b"; wt_b.mkdir()
    state = WorkspaceState(tasks={
        "A": _task("A", wt_a),
        "B": _task("B", wt_b),
    })
    outside = tmp_path / "elsewhere"; outside.mkdir()
    monkeypatch.chdir(outside)
    output, err = _tty_output()
    with pytest.raises(typer.Exit):
        resolve_for_command("finish", state, cli_task=None, output=output)
    text = err.getvalue()
    assert "ambiguous" in text.lower() or "multiple" in text.lower()
    assert "--task A" in text
    assert "--task B" in text

from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.state import Task, WorkspaceState
from mship.core.task_resolver import (
    resolve_task,
    NoActiveTaskError,
    UnknownTaskError,
    AmbiguousTaskError,
)


def _task(slug: str, worktrees: dict[str, Path]) -> Task:
    return Task(
        slug=slug,
        description=f"desc for {slug}",
        phase="plan",
        created_at=datetime(2026, 4, 16, tzinfo=timezone.utc),
        affected_repos=list(worktrees.keys()),
        branch=f"feat/{slug}",
        worktrees={k: Path(v) for k, v in worktrees.items()},
    )


def test_cli_task_match(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    state = WorkspaceState(tasks={"A": _task("A", {"r": wt})})
    t, _ = resolve_task(state, cli_task="A", env_task=None, cwd=tmp_path)
    assert t.slug == "A"


def test_cli_task_miss_raises(tmp_path: Path):
    state = WorkspaceState(tasks={"A": _task("A", {})})
    with pytest.raises(UnknownTaskError) as exc:
        resolve_task(state, cli_task="B", env_task=None, cwd=tmp_path)
    assert exc.value.slug == "B"


def test_env_task_match(tmp_path: Path):
    state = WorkspaceState(tasks={"A": _task("A", {})})
    t, _ = resolve_task(state, cli_task=None, env_task="A", cwd=tmp_path)
    assert t.slug == "A"


def test_env_task_miss_raises(tmp_path: Path):
    state = WorkspaceState(tasks={"A": _task("A", {})})
    with pytest.raises(UnknownTaskError):
        resolve_task(state, cli_task=None, env_task="C", cwd=tmp_path)


def test_cwd_inside_worktree_resolves(tmp_path: Path):
    wt = tmp_path / "A_wt"; wt.mkdir()
    state = WorkspaceState(tasks={"A": _task("A", {"r": wt})})
    t, _ = resolve_task(state, cli_task=None, env_task=None, cwd=wt)
    assert t.slug == "A"


def test_cwd_deep_inside_worktree_resolves(tmp_path: Path):
    wt = tmp_path / "A_wt"; wt.mkdir()
    deep = wt / "src" / "foo"; deep.mkdir(parents=True)
    state = WorkspaceState(tasks={"A": _task("A", {"r": wt})})
    t, _ = resolve_task(state, cli_task=None, env_task=None, cwd=deep)
    assert t.slug == "A"


def test_zero_tasks_raises_no_active(tmp_path: Path):
    state = WorkspaceState(tasks={})
    with pytest.raises(NoActiveTaskError):
        resolve_task(state, cli_task=None, env_task=None, cwd=tmp_path)


def test_two_tasks_no_anchor_raises_ambiguous(tmp_path: Path):
    state = WorkspaceState(tasks={
        "A": _task("A", {}),
        "B": _task("B", {}),
    })
    with pytest.raises(AmbiguousTaskError) as exc:
        resolve_task(state, cli_task=None, env_task=None, cwd=tmp_path)
    assert exc.value.active == ["A", "B"]


def test_one_task_no_anchor_auto_resolves(tmp_path: Path):
    """With exactly one active task and no anchor, use it — no ambiguity."""
    state = WorkspaceState(tasks={"only": _task("only", {})})
    t, _ = resolve_task(state, cli_task=None, env_task=None, cwd=tmp_path)
    assert t.slug == "only"


def test_three_tasks_no_anchor_still_raises_ambiguous(tmp_path: Path):
    """Auto-resolve is ONLY for exactly one task."""
    state = WorkspaceState(tasks={
        "A": _task("A", {}),
        "B": _task("B", {}),
        "C": _task("C", {}),
    })
    with pytest.raises(AmbiguousTaskError) as exc:
        resolve_task(state, cli_task=None, env_task=None, cwd=tmp_path)
    assert exc.value.active == ["A", "B", "C"]


def test_flag_beats_env_beats_cwd(tmp_path: Path):
    wtA = tmp_path / "A_wt"; wtA.mkdir()
    state = WorkspaceState(tasks={
        "A": _task("A", {"r": wtA}),
        "B": _task("B", {}),
        "C": _task("C", {}),
    })
    # cwd inside A_wt, env="B", flag="C" -> flag wins
    assert resolve_task(state, cli_task="C", env_task="B", cwd=wtA)[0].slug == "C"
    # env="B", flag=None, cwd inside A_wt -> env wins
    assert resolve_task(state, cli_task=None, env_task="B", cwd=wtA)[0].slug == "B"
    # env=None, flag=None, cwd inside A_wt -> cwd wins
    assert resolve_task(state, cli_task=None, env_task=None, cwd=wtA)[0].slug == "A"


# ---------------------------------------------------------------------------
# New tests for ResolutionSource and upgraded AmbiguousTaskError
# ---------------------------------------------------------------------------

from mship.core.task_resolver import ResolutionSource


def test_cli_task_source_is_cli_flag(tmp_path: Path):
    state = WorkspaceState(tasks={"A": _task("A", {})})
    task, source = resolve_task(state, cli_task="A", env_task=None, cwd=tmp_path)
    assert task.slug == "A"
    assert source == ResolutionSource.CLI_FLAG


def test_env_source_is_env_var(tmp_path: Path):
    state = WorkspaceState(tasks={"A": _task("A", {})})
    task, source = resolve_task(state, cli_task=None, env_task="A", cwd=tmp_path)
    assert source == ResolutionSource.ENV_VAR


def test_cwd_source_when_inside_worktree(tmp_path: Path):
    wt = tmp_path / "wt"
    wt.mkdir()
    state = WorkspaceState(tasks={"A": _task("A", {"r": wt})})
    task, source = resolve_task(state, cli_task=None, env_task=None, cwd=wt)
    assert task.slug == "A"
    assert source == ResolutionSource.CWD


def test_single_active_source_when_no_anchor(tmp_path: Path):
    """One active task, cwd is outside — returns it with SINGLE_ACTIVE source."""
    state = WorkspaceState(tasks={"A": _task("A", {"r": tmp_path / "elsewhere"})})
    task, source = resolve_task(
        state, cli_task=None, env_task=None, cwd=tmp_path,
    )
    assert task.slug == "A"
    assert source == ResolutionSource.SINGLE_ACTIVE


def test_cwd_inside_multiple_worktrees_raises_ambiguity(tmp_path: Path):
    """Cwd is under two different tasks' worktrees → error, not silent pick."""
    shared = tmp_path / "shared"
    shared.mkdir()
    # Two tasks both claim the same path as a worktree.
    state = WorkspaceState(tasks={
        "A": _task("A", {"r": shared}),
        "B": _task("B", {"r": shared}),
    })
    with pytest.raises(AmbiguousTaskError) as exc:
        resolve_task(state, cli_task=None, env_task=None, cwd=shared)
    # Both candidates surface with their worktree paths.
    slugs = [c[0] for c in exc.value.candidates]
    assert set(slugs) == {"A", "B"}


def test_no_anchor_multi_task_error_carries_candidates(tmp_path: Path):
    """Existing no-anchor case now also populates candidates for better errors."""
    wt_a = tmp_path / "a"; wt_a.mkdir()
    wt_b = tmp_path / "b"; wt_b.mkdir()
    state = WorkspaceState(tasks={
        "A": _task("A", {"r": wt_a}),
        "B": _task("B", {"r": wt_b}),
    })
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(AmbiguousTaskError) as exc:
        resolve_task(state, cli_task=None, env_task=None, cwd=outside)
    slugs = [c[0] for c in exc.value.candidates]
    assert set(slugs) == {"A", "B"}

"""Concurrency tests proving lost-update prevention across tasks.

These retain the original command-level race coverage alongside the normalized
repository contracts. They use spawn-context multiprocessing so every worker
opens its own SQLite connection — the exact scenario concurrent `mship`
invocations create on every supported platform.
"""
from __future__ import annotations

import multiprocessing
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.config import ConfigLoader
from mship.core.graph import DependencyGraph
from mship.core.log import LogManager
from mship.core.phase import PhaseManager
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.worktree import WorktreeManager
from mship.util.git import GitRunner
from mship.util.shell import ShellRunner, ShellResult


PROCESS_TIMEOUT_SECONDS = 10


def _join_or_terminate(process) -> None:
    process.join(timeout=PROCESS_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(timeout=PROCESS_TIMEOUT_SECONDS)
        pytest.fail(f"worker exceeded {PROCESS_TIMEOUT_SECONDS}s")
    assert process.exitcode == 0, f"worker crashed (exitcode={process.exitcode})"


# ---------------------------------------------------------------------------
# Phase transition race: two tasks, two workers, both must land correctly.
# ---------------------------------------------------------------------------

def _phase_worker(state_dir_str: str, slug: str, target: str) -> None:
    state_dir = Path(state_dir_str)
    sm = StateManager(state_dir)
    pm = PhaseManager(sm, MagicMock(spec=LogManager))
    pm.transition(slug, target)


def test_concurrent_phase_transitions_do_not_lose_updates(tmp_path: Path):
    """Two processes transition DIFFERENT tasks concurrently — both must stick."""
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    sm = StateManager(state_dir)

    created = datetime(2026, 4, 16, tzinfo=timezone.utc)
    state = WorkspaceState(
        tasks={
            "a": Task(slug="a", description="A", phase="plan",
                      created_at=created, affected_repos=["shared"], branch="feat/a"),
            "b": Task(slug="b", description="B", phase="plan",
                      created_at=created, affected_repos=["shared"], branch="feat/b"),
        },
    )
    sm.save(state)

    # Fire a reasonable number of pairs to amplify any lost-update race.
    ctx = multiprocessing.get_context("spawn")
    pairs = 8
    procs = []
    for i in range(pairs):
        procs.append(ctx.Process(
            target=_phase_worker, args=(str(state_dir), "a", "dev"),
        ))
        procs.append(ctx.Process(
            target=_phase_worker, args=(str(state_dir), "b", "review"),
        ))
    for p in procs:
        p.start()
    for p in procs:
        _join_or_terminate(p)

    final = sm.load()
    # Without mutate(), one task's update could have been clobbered by the
    # other's save. Both final phases must reflect the last transition each
    # worker intended.
    assert final.tasks["a"].phase == "dev"
    assert final.tasks["b"].phase == "review"


# ---------------------------------------------------------------------------
# Spawn duplicate-slug race: two concurrent spawns with the same slug — exactly
# one must win, the other must raise.
# ---------------------------------------------------------------------------

def _spawn_worker(state_dir_str: str, result_path_str: str, slug_desc: str) -> None:
    """Independent subprocess body: initialises its own state manager +
    worktree manager, then tries to mutate-in a task with `slug_desc`.

    We skip the filesystem/git side (which spawn() normally does) by calling
    `StateManager.mutate()` directly with the same check-and-set contract
    that `WorktreeManager.spawn` performs inside its `_apply`. This isolates
    the atomic-check guarantee without needing a git-repo-per-subprocess.
    """
    state_dir = Path(state_dir_str)
    sm = StateManager(state_dir)
    from mship.util.slug import slugify
    slug = slugify(slug_desc)

    def _apply(s: WorkspaceState) -> None:
        if slug in s.tasks:
            raise ValueError(f"Task '{slug}' already exists.")
        s.tasks[slug] = Task(
            slug=slug,
            description=slug_desc,
            phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=[],
            branch=f"feat/{slug}",
        )

    result_path = Path(result_path_str)
    try:
        sm.mutate(_apply)
        result_path.write_text("ok")
    except ValueError:
        result_path.write_text("duplicate")


def test_concurrent_same_slug_spawns_exactly_one_wins(tmp_path: Path):
    """Two processes attempt to register the same slug concurrently. The
    atomic check inside mutate() must ensure exactly one succeeds and the
    other raises."""
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    sm = StateManager(state_dir)
    sm.save(WorkspaceState())

    r1 = tmp_path / "r1.txt"
    r2 = tmp_path / "r2.txt"

    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(
        target=_spawn_worker, args=(str(state_dir), str(r1), "same desc"),
    )
    p2 = ctx.Process(
        target=_spawn_worker, args=(str(state_dir), str(r2), "same desc"),
    )
    p1.start()
    p2.start()
    _join_or_terminate(p1)
    _join_or_terminate(p2)

    outcomes = sorted([r1.read_text(), r2.read_text()])
    assert outcomes == ["duplicate", "ok"], f"expected one ok + one duplicate, got {outcomes}"

    state = sm.load()
    assert "same-desc" in state.tasks
    assert len(state.tasks) == 1


# ---------------------------------------------------------------------------
# WorktreeManager.spawn TOCTOU: invoke the real spawn path (not just mutate())
# from two processes so the in-lock check-and-set is exercised end-to-end.
# ---------------------------------------------------------------------------

def _real_spawn_worker(workspace_str: str, result_path_str: str, desc: str) -> None:
    workspace = Path(workspace_str)
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    sm = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    log = MagicMock(spec=LogManager)

    mgr = WorktreeManager(config, graph, sm, git, shell, log)
    result_path = Path(result_path_str)
    try:
        mgr.spawn(desc, repos=["shared"], workspace_root=workspace)
        result_path.write_text("ok")
    except ValueError as e:
        # Expected error on the losing racer
        assert "already exists" in str(e)
        result_path.write_text("duplicate")
    except Exception as e:  # pragma: no cover — useful diagnostics if this fires
        result_path.write_text(f"error:{type(e).__name__}:{e}")


def test_real_spawn_concurrent_same_slug(workspace_with_git: Path):
    """End-to-end: two processes call WorktreeManager.spawn() with the same
    description. The critical guarantee is that the mship state ends up with
    exactly one registered task — no lost-update race can cause both spawns
    to register.

    Git-side contention (one worker creates the worktree/branch dir first and
    the second hits a filesystem or git-level collision) is an acceptable
    failure mode for the loser, because the in-mutate check is what protects
    the state file itself.
    """
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)

    r1 = workspace_with_git / "r1.txt"
    r2 = workspace_with_git / "r2.txt"

    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(
        target=_real_spawn_worker,
        args=(str(workspace_with_git), str(r1), "race me"),
    )
    p2 = ctx.Process(
        target=_real_spawn_worker,
        args=(str(workspace_with_git), str(r2), "race me"),
    )
    p1.start()
    p2.start()
    _join_or_terminate(p1)
    _join_or_terminate(p2)

    outcomes = sorted([r1.read_text(), r2.read_text()])
    # At least one spawn must succeed (otherwise something else is broken),
    # and we must NOT see two "ok" outcomes (which would indicate a
    # lost-update race past the transactional check-and-set).
    ok_count = sum(1 for o in outcomes if o == "ok")
    assert ok_count >= 1, f"neither spawn succeeded: {outcomes}"
    assert ok_count == 1, (
        f"two spawns both registered the same slug (lost-update race): {outcomes}"
    )

    sm = StateManager(state_dir)
    state = sm.load()
    # Exactly one task registered under the shared slug.
    assert "race-me" in state.tasks
    assert len(state.tasks) == 1

"""Explicit workspace context (#472): everything the DI container wires, built
from an explicit `config_path` with ZERO discovery — no `Path.cwd()`, no
`MSHIP_WORKSPACE`, no marker walk-up. This is what kills ambient workspace
state on daemon paths; `get_container` (cli/__init__.py) keeps discovery for
the interactive CLI.

`_resolve_state_dir` moved here from `mship.cli.__init__` (a re-export remains
there for existing importers). Its `os.environ` iteration STRIPS git env vars
before the subprocess call — it never selects a workspace from env — which is
why the ambient-invariants sweep allowlists it as "strips-not-selects".
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mship.core.config import ConfigLoader, WorkspaceConfig


class ContextError(Exception):
    """A workspace context could not be built (missing/invalid config)."""


def _resolve_state_dir(config_path):
    """Resolve the effective state owner.

    A config at a checkout root shares the main checkout's state across linked
    worktrees. A nested workspace config owns its local `.mothership`, even
    when an enclosing repository contains multiple sibling workspaces.
    """
    config_path = Path(config_path)
    try:
        # Strip GIT_DIR / GIT_COMMON_DIR so git re-discovers from the workspace
        # dir rather than inheriting a worktree-specific git dir set by a parent
        # git hook process. (Strips env; never selects a workspace from it.)
        env = {k: v for k, v in os.environ.items()
               if k not in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE")}
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "--git-common-dir"],
            cwd=config_path.parent,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        lines = result.stdout.splitlines()
        if len(lines) != 2:
            raise ValueError("unexpected git rev-parse output")
        checkout_root = Path(lines[0]).resolve()
        if config_path.parent.resolve() != checkout_root:
            return config_path.parent / ".mothership"
        git_common_dir = Path(lines[1])
        if not git_common_dir.is_absolute():
            git_common_dir = (config_path.parent / git_common_dir).resolve()
        return git_common_dir.parent / ".mothership"
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError):
        return config_path.parent / ".mothership"


@dataclass(frozen=True)
class WorkspaceContext:
    config_path: Path
    workspace_root: Path
    config: WorkspaceConfig
    state_dir: Path
    state_manager: object
    log_manager: object
    worktree_manager: object


def build_workspace_context(config_path: Path) -> WorkspaceContext:
    """Replicates exactly what `Container` wires, minus discovery. Raises typed
    `ContextError` on a missing/invalid config so degraded registry entries
    fail loud at the boundary instead of leaking arbitrary exceptions."""
    from mship.core.graph import DependencyGraph
    from mship.core.log import LogManager
    from mship.core.state import StateManager
    from mship.core.worktree import WorktreeManager
    from mship.util.git import GitRunner
    from mship.util.shell import ShellRunner

    config_path = Path(config_path)
    if not config_path.is_file():
        raise ContextError(f"no mothership.yaml at {config_path}")
    try:
        # require_paths=False mirrors discovery (discovery.py::_materialize):
        # a workspace with one materialized repo and one not-yet-cloned repo is
        # HEALTHY there, so loading strictly here would 500 the first request
        # to a workspace the registry advertises as serveable.
        config = ConfigLoader.load(config_path, require_paths=False)
    except Exception as e:
        raise ContextError(f"invalid workspace config {config_path}: {e}") from e

    state_dir = _resolve_state_dir(config_path)
    state_manager = StateManager(state_dir=state_dir)
    log_manager = LogManager(logs_dir=state_dir / "logs")
    graph = DependencyGraph(config=config)
    worktree_manager = WorktreeManager(
        config=config,
        graph=graph,
        state_manager=state_manager,
        git=GitRunner(),
        shell=ShellRunner(),
        log=log_manager,
    )
    return WorkspaceContext(
        config_path=config_path,
        workspace_root=config_path.parent,
        config=config,
        state_dir=state_dir,
        state_manager=state_manager,
        log_manager=log_manager,
        worktree_manager=worktree_manager,
    )

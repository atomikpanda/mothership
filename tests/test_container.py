from pathlib import Path

import pytest

from mship.container import Container
from mship.core.config import WorkspaceConfig
from mship.core.executor import RepoExecutor
from mship.core.graph import DependencyGraph
from mship.core.phase import PhaseManager
from mship.core.state import StateManager
from mship.core.worktree import WorktreeManager


def test_container_wires_config(workspace: Path):
    container = Container()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    config = container.config()
    assert isinstance(config, WorkspaceConfig)
    assert config.workspace == "test-platform"


def test_container_wires_graph(workspace: Path):
    container = Container()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    graph = container.graph()
    assert isinstance(graph, DependencyGraph)


def test_container_wires_state_manager(workspace: Path):
    container = Container()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    mgr = container.state_manager()
    assert isinstance(mgr, StateManager)


def test_container_wires_executor(workspace: Path):
    container = Container()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    executor = container.executor()
    assert isinstance(executor, RepoExecutor)


def test_container_wires_worktree_manager(workspace: Path):
    container = Container()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    mgr = container.worktree_manager()
    assert isinstance(mgr, WorktreeManager)


def test_container_wires_phase_manager(workspace: Path):
    container = Container()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    mgr = container.phase_manager()
    assert isinstance(mgr, PhaseManager)

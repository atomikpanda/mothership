from dependency_injector import containers, providers

from mship.core.config import ConfigLoader, WorkspaceConfig
from mship.core.executor import RepoExecutor
from mship.core.graph import DependencyGraph
from mship.core.phase import PhaseManager
from mship.core.state import StateManager
from mship.core.worktree import WorktreeManager
from mship.util.git import GitRunner
from mship.util.shell import ShellRunner


class Container(containers.DeclarativeContainer):
    config_path = providers.Dependency(instance_of=object)
    state_dir = providers.Dependency(instance_of=object)

    config = providers.Singleton(
        ConfigLoader.load,
        path=config_path,
    )

    state_manager = providers.Singleton(
        StateManager,
        state_dir=state_dir,
    )

    git = providers.Singleton(GitRunner)

    shell = providers.Singleton(ShellRunner)

    graph = providers.Factory(
        DependencyGraph,
        config=config,
    )

    executor = providers.Factory(
        RepoExecutor,
        config=config,
        graph=graph,
        state_manager=state_manager,
        shell=shell,
    )

    worktree_manager = providers.Factory(
        WorktreeManager,
        config=config,
        graph=graph,
        state_manager=state_manager,
        git=git,
        shell=shell,
    )

    phase_manager = providers.Factory(
        PhaseManager,
        state_manager=state_manager,
    )

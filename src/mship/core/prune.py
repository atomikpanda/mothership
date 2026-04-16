import shutil
from dataclasses import dataclass
from pathlib import Path

from mship.core.config import WorkspaceConfig
from mship.core.state import StateManager
from mship.util.git import GitRunner


@dataclass
class OrphanedWorktree:
    repo: str
    path: Path
    reason: str  # "not_in_state" | "not_on_disk"


class PruneManager:
    """Detect and clean up orphaned worktrees."""

    def __init__(
        self,
        config: WorkspaceConfig,
        state_manager: StateManager,
        git: GitRunner,
    ) -> None:
        self._config = config
        self._state_manager = state_manager
        self._git = git

    def scan(self) -> list[OrphanedWorktree]:
        orphans: list[OrphanedWorktree] = []

        # Collect all worktree paths tracked in state
        state = self._state_manager.load()
        tracked_paths: set[str] = set()
        for task in state.tasks.values():
            for repo_name, wt_path in task.worktrees.items():
                tracked_paths.add(str(Path(wt_path).resolve()))

        # Scan filesystem for worktrees not in state
        for repo_name, repo_config in self._config.repos.items():
            worktrees_dir = repo_config.path / ".worktrees"
            if not worktrees_dir.exists():
                continue
            for wt_path in self._walk_worktrees(worktrees_dir):
                resolved = str(wt_path.resolve())
                if resolved not in tracked_paths:
                    orphans.append(OrphanedWorktree(
                        repo=repo_name,
                        path=wt_path,
                        reason="not_in_state",
                    ))

        # Check state entries pointing to nonexistent worktrees
        for task_slug, task in state.tasks.items():
            for repo_name, wt_path in task.worktrees.items():
                if not Path(wt_path).exists():
                    orphans.append(OrphanedWorktree(
                        repo=repo_name,
                        path=Path(wt_path),
                        reason="not_on_disk",
                    ))

        return orphans

    def prune(self, orphans: list[OrphanedWorktree]) -> int:
        pruned = 0

        # Phase 1: remove on-disk orphans (not in state)
        for orphan in orphans:
            if orphan.reason == "not_in_state":
                repo_config = self._config.repos.get(orphan.repo)
                if repo_config:
                    try:
                        self._git.worktree_remove(
                            repo_path=repo_config.path,
                            worktree_path=orphan.path,
                        )
                    except Exception:
                        shutil.rmtree(orphan.path, ignore_errors=True)
                pruned += 1

        # Phase 2: clean up state entries pointing to nonexistent worktrees
        def _cleanup(state):
            nonlocal pruned
            for orphan in orphans:
                if orphan.reason != "not_on_disk":
                    continue
                for task_slug, task in list(state.tasks.items()):
                    if orphan.repo in task.worktrees:
                        wt_path = task.worktrees[orphan.repo]
                        if not Path(wt_path).exists():
                            # Remove just the worktree entry, not the entire task
                            del task.worktrees[orphan.repo]
                            pruned += 1
                            # If task has no worktrees left, remove the task
                            if not task.worktrees:
                                del state.tasks[task_slug]
                            break

        self._state_manager.mutate(_cleanup)

        # Run git worktree prune per repo
        for repo_config in self._config.repos.values():
            self._git.run_worktree_prune(repo_config.path)

        return pruned

    def _walk_worktrees(self, worktrees_dir: Path) -> list[Path]:
        """Find worktree directories (contain a .git file)."""
        results: list[Path] = []
        for item in worktrees_dir.rglob(".git"):
            if item.is_file():  # worktrees have a .git file, not directory
                results.append(item.parent)
        return results

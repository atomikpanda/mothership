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

        # Hub layout: scan <workspace>/.worktrees/<slug>/<repo>/
        # Workspace root is not stored on WorkspaceConfig, so derive it from
        # each configured repo. In a single-repo workspace the repo path may
        # itself be the workspace root (for example "."), so use repo_cfg.path
        # when it contains mothership.yaml; otherwise fall back to its parent.
        candidates: set[Path] = set()
        for repo_cfg in self._config.repos.values():
            repo_path = repo_cfg.path.resolve()
            ws_root = repo_path if (repo_path / "mothership.yaml").exists() else repo_path.parent
            candidates.add(ws_root)
        for ws_root in candidates:
            hub_root = ws_root / ".worktrees"
            if not hub_root.is_dir():
                continue
            for slug_dir in hub_root.iterdir():
                if not slug_dir.is_dir():
                    continue
                for repo_dir in slug_dir.iterdir():
                    if not repo_dir.is_dir():
                        continue
                    if not (repo_dir / ".git").exists():
                        continue
                    resolved = str(repo_dir.resolve())
                    if resolved in tracked_paths:
                        continue
                    # Hub worktrees are created under <hub>/<slug>/<repo_name>/,
                    # so prefer matching the directory name as a configured repo
                    # key. Fall back to the canonical path name for compatibility
                    # with older or unusual layouts.
                    matched_repo = repo_dir.name if repo_dir.name in self._config.repos else None
                    if matched_repo is None:
                        for name, rc in self._config.repos.items():
                            if rc.path.name == repo_dir.name:
                                matched_repo = name
                                break
                    if matched_repo is None:
                        continue
                    orphans.append(OrphanedWorktree(
                        repo=matched_repo,
                        path=repo_dir,
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

        # Retain metadata before entering the state lock. WorkItemStore's item
        # lock must never be taken while holding state.lock, and a persistence
        # failure must leave state (the last copy) untouched.
        from mship.core.workitem_lifecycle import (
            require_retained_task_metadata,
            retain_workitem_metadata_on_teardown,
        )

        state_before_cleanup = self._state_manager.load()
        retained_by_slug = {}
        workitems_dir = self._state_manager.state_dir / "workitems"
        for task in state_before_cleanup.tasks.values():
            remaining = {
                repo: path
                for repo, path in task.worktrees.items()
                if not any(
                    orphan.reason == "not_on_disk"
                    and orphan.repo == repo
                    and not Path(path).exists()
                    for orphan in orphans
                )
            }
            if task.worktrees and not remaining:
                retained_by_slug[task.slug] = retain_workitem_metadata_on_teardown(
                    task=task, workitems_dir=workitems_dir,
                )

        # Phase 2: clean up state entries pointing to nonexistent worktrees.
        # Validate every task that will be deleted before mutating state, so a
        # concurrent metadata update leaves the entire source snapshot intact.
        def _cleanup(state):
            nonlocal pruned
            cleanup_actions = []
            for task_slug, task in state.tasks.items():
                missing_repos = [
                    repo
                    for repo, path in task.worktrees.items()
                    if any(
                        orphan.reason == "not_on_disk"
                        and orphan.repo == repo
                        and not Path(path).exists()
                        for orphan in orphans
                    )
                ]
                if not missing_repos:
                    continue
                removes_task = len(missing_repos) == len(task.worktrees)
                if removes_task:
                    require_retained_task_metadata(
                        task, retained_by_slug.get(task_slug),
                    )
                cleanup_actions.append((task_slug, missing_repos, removes_task))

            for task_slug, missing_repos, removes_task in cleanup_actions:
                task = state.tasks[task_slug]
                for repo in missing_repos:
                    del task.worktrees[repo]
                    pruned += 1
                if removes_task:
                    del state.tasks[task_slug]

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

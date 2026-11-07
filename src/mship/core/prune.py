from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mship.core.config import WorkspaceConfig
from mship.core.retained_worktrees import RetainedWorktreeStore
from mship.core.state import StateManager
from mship.core.workspace_marker import MARKER_NAME
from mship.util.git import GitRunner, WorktreeRemovalRefused


@dataclass(frozen=True)
class OrphanedWorktree:
    repo: str
    path: Path
    reason: str  # "not_in_state" | "not_on_disk" | "retained"
    retained_record: Path | None = None


@dataclass(frozen=True)
class RemovedWorktree:
    repo: str
    path: Path
    recovery_ref: str


@dataclass(frozen=True)
class PruneRefusal:
    repo: str
    path: Path
    reason: str


@dataclass
class PruneResult:
    removed: list[RemovedWorktree] = field(default_factory=list)
    refusals: list[PruneRefusal] = field(default_factory=list)
    stale_mappings_removed: int = 0

    @property
    def count(self) -> int:
        return len(self.removed) + self.stale_mappings_removed

    @property
    def recovery_refs(self) -> list[str]:
        return [removed.recovery_ref for removed in self.removed]

    @property
    def remaining_paths(self) -> list[Path]:
        return [
            refusal.path
            for refusal in self.refusals
            if refusal.path.exists()
        ]


class WritersNotStoppedError(RuntimeError):
    """Prune cleanup needs an explicit acknowledgement that writers are stopped."""


class PruneManager:
    """Detect and conservatively clean up orphaned worktrees."""

    def __init__(
        self,
        config: WorkspaceConfig,
        state_manager: StateManager,
        git: GitRunner,
    ) -> None:
        self._config = config
        self._state_manager = state_manager
        self._git = git

    @staticmethod
    def _path_key(path: Path) -> str:
        try:
            return str(Path(path).resolve())
        except (OSError, RuntimeError):
            return str(Path(path).absolute())

    def scan(self) -> list[OrphanedWorktree]:
        state = self._state_manager.load()
        active_paths = {
            self._path_key(Path(worktree_path))
            for task in state.tasks.values()
            for worktree_path in task.worktrees.values()
        }
        canonical_paths = {
            self._path_key(repo_config.path)
            for repo_config in self._config.repos.values()
        }
        found: dict[str, OrphanedWorktree] = {}

        def remember(
            repo: str,
            path: Path,
            reason: str,
            *,
            retained_record: Path | None = None,
            skip_active: bool = True,
        ) -> None:
            key = self._path_key(path)
            if key in canonical_paths or (skip_active and key in active_paths):
                return
            candidate = OrphanedWorktree(
                repo=repo,
                path=path,
                reason=reason,
                retained_record=retained_record,
            )
            current = found.get(key)
            if current is None or reason == "retained":
                found[key] = candidate

        retained_store = RetainedWorktreeStore(self._state_manager.state_dir)
        for record_path, retained_task in retained_store.list_records():
            for repo_name, worktree_path in retained_task.worktrees.items():
                repo_config = self._config.repos.get(repo_name)
                if repo_config is not None and repo_config.git_root is not None:
                    continue
                remember(
                    repo_name,
                    Path(worktree_path),
                    "retained",
                    retained_record=record_path,
                )

        # Legacy per-repository layout: <repo>/.worktrees/<slug>/<repo>.
        for repo_name, repo_config in self._config.repos.items():
            if repo_config.git_root is not None:
                continue
            worktrees_dir = repo_config.path / ".worktrees"
            if not worktrees_dir.exists():
                continue
            for worktree_path in self._walk_worktrees(worktrees_dir):
                remember(repo_name, worktree_path, "not_in_state")

        # Hub layout: <workspace>/.worktrees/<slug>/<repo>/.
        workspace_roots: set[Path] = set()
        for repo_config in self._config.repos.values():
            repo_path = repo_config.path.resolve()
            workspace_roots.add(
                repo_path if (repo_path / "mothership.yaml").exists() else repo_path.parent
            )
        for workspace_root in workspace_roots:
            hub_root = workspace_root / ".worktrees"
            if not hub_root.is_dir():
                continue
            for slug_dir in hub_root.iterdir():
                if not slug_dir.is_dir():
                    continue
                for repo_dir in slug_dir.iterdir():
                    if not repo_dir.is_dir() or not (repo_dir / ".git").is_file():
                        continue
                    matched_repo = self._hub_repo_name(repo_dir)
                    if matched_repo is None:
                        continue
                    repo_config = self._config.repos[matched_repo]
                    if repo_config.git_root is not None:
                        continue
                    remember(matched_repo, repo_dir, "not_in_state")

        # Missing active mappings are metadata-only cleanup candidates. Keep
        # matching exact (repo, path) pairs so one missing repo cannot erase a
        # different task's mapping.
        for task in state.tasks.values():
            for repo_name, worktree_path in task.worktrees.items():
                repo_config = self._config.repos.get(repo_name)
                if (
                    repo_config is not None
                    and repo_config.git_root is not None
                ):
                    continue
                if not Path(worktree_path).exists():
                    remember(
                        repo_name,
                        Path(worktree_path),
                        "not_on_disk",
                        skip_active=False,
                    )

        return list(found.values())

    def prune(
        self,
        orphans: list[OrphanedWorktree],
        *,
        writers_stopped: bool = False,
    ) -> PruneResult:
        """Clean only with an explicit stopped-writers acknowledgement."""
        if writers_stopped is not True:
            raise WritersNotStoppedError(
                "Refusing to prune: pass writers_stopped=True after stopping writers"
            )

        result = PruneResult()
        for orphan in orphans:
            if orphan.reason not in {"not_in_state", "retained"}:
                continue
            repo_config = self._config.repos.get(orphan.repo)
            if repo_config is None:
                result.refusals.append(
                    PruneRefusal(orphan.repo, orphan.path, "repository is not configured")
                )
                continue
            if repo_config.git_root is not None:
                result.refusals.append(
                    PruneRefusal(orphan.repo, orphan.path, "repository is a git-root alias")
                )
                continue
            if not orphan.path.exists():
                result.refusals.append(
                    PruneRefusal(orphan.repo, orphan.path, "worktree path no longer exists")
                )
                continue
            try:
                recovery_ref = self._git.worktree_remove(
                    repo_path=repo_config.path,
                    worktree_path=orphan.path,
                )
            except WorktreeRemovalRefused as exc:
                result.refusals.append(PruneRefusal(orphan.repo, orphan.path, str(exc)))
                continue
            if orphan.path.exists():
                result.refusals.append(
                    PruneRefusal(
                        orphan.repo,
                        orphan.path,
                        "git reported removal but the worktree path remains",
                    )
                )
                continue
            result.removed.append(
                RemovedWorktree(orphan.repo, orphan.path, recovery_ref)
            )
            self._remove_empty_hub(orphan.path)

        state_before_cleanup = self._state_manager.load()
        missing_pairs = {
            (orphan.repo, self._path_key(orphan.path))
            for orphan in orphans
            if orphan.reason == "not_on_disk"
        }
        missing_by_slug: dict[str, list[str]] = {}
        for task in state_before_cleanup.tasks.values():
            missing = [
                repo_name
                for repo_name, worktree_path in task.worktrees.items()
                if (
                    (repo_name, self._path_key(Path(worktree_path))) in missing_pairs
                    and not Path(worktree_path).exists()
                )
            ]
            if missing:
                missing_by_slug[task.slug] = missing

        # Filesystem observations remain outside SQL. Each Task snapshot is
        # compared after the write transaction begins, so a concurrent update
        # is rejected instead of being overwritten by stale prune results.
        from mship.core.persistence.lifecycle_repository import LifecycleRepository

        lifecycle = LifecycleRepository(self._state_manager.workspace_store)
        recovery_store = RetainedWorktreeStore(self._state_manager.state_dir)
        for task_slug, missing_repos in missing_by_slug.items():
            # Missing paths may be offline or moved, not empty. Preserve the
            # original mappings before dropping any active-state references.
            recovery_store.retain(state_before_cleanup.tasks[task_slug])
            lifecycle.retain_and_prune_task_repos(
                task_slug,
                missing_repos,
                now=datetime.now(timezone.utc),
                expected_task=state_before_cleanup.tasks[task_slug],
            )
            result.stale_mappings_removed += len(missing_repos)

        return result

    def _hub_repo_name(self, repo_dir: Path) -> str | None:
        if repo_dir.name in self._config.repos:
            return repo_dir.name
        for name, repo_config in self._config.repos.items():
            if repo_config.path.name == repo_dir.name:
                return name
        return None

    def _remove_empty_hub(self, worktree_path: Path) -> None:
        """Remove only an empty task hub, never recursively delete its contents."""
        hub = worktree_path.parent
        if hub.parent.name != ".worktrees":
            return
        try:
            hub.rmdir()
            return
        except OSError:
            pass

        marker = hub / MARKER_NAME
        workspace_root = hub.parent.parent
        if (
            marker.is_symlink()
            or not marker.is_file()
            or not (workspace_root / "mothership.yaml").is_file()
        ):
            return
        try:
            if marker.read_text() != f"{workspace_root.resolve()}\n":
                return
            marker.unlink()
            hub.rmdir()
        except OSError:
            return

    def _walk_worktrees(self, worktrees_dir: Path) -> list[Path]:
        """Find worktree directories (contain a .git file)."""
        results: list[Path] = []
        for item in worktrees_dir.rglob(".git"):
            if item.is_file():
                results.append(item.parent)
        return results

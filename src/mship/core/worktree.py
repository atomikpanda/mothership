from datetime import datetime, timezone
from pathlib import Path

from mship.core.config import WorkspaceConfig
from mship.core.graph import DependencyGraph
from mship.core.log import LogManager
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.git import GitRunner
from mship.util.shell import ShellRunner
from mship.util.slug import slugify


class WorktreeManager:
    """Cross-repo worktree orchestration."""

    def __init__(
        self,
        config: WorkspaceConfig,
        graph: DependencyGraph,
        state_manager: StateManager,
        git: GitRunner,
        shell: ShellRunner,
        log: LogManager,
    ) -> None:
        self._config = config
        self._graph = graph
        self._state_manager = state_manager
        self._git = git
        self._shell = shell
        self._log = log

    def spawn(
        self,
        description: str,
        repos: list[str] | None = None,
    ) -> Task:
        slug = slugify(description)
        branch = self._config.branch_pattern.replace("{slug}", slug)

        state = self._state_manager.load()
        if slug in state.tasks:
            raise ValueError(
                f"Task '{slug}' already exists. "
                f"Run `mship abort --yes` to remove it first, or use a different description."
            )

        if repos is None:
            repos = list(self._config.repos.keys())

        ordered = self._graph.topo_sort(repos)

        worktrees: dict[str, Path] = {}
        for repo_name in ordered:
            repo_config = self._config.repos[repo_name]

            if repo_config.git_root is not None:
                # Subdirectory service: share parent's worktree
                parent_wt = worktrees.get(repo_config.git_root)
                if parent_wt is None:
                    # Parent wasn't spawned in this call — use its config path
                    parent_wt = self._config.repos[repo_config.git_root].path
                effective = parent_wt / repo_config.path
                worktrees[repo_name] = effective

                # Run setup task in the subdirectory
                actual_setup = repo_config.tasks.get("setup", "setup")
                self._shell.run_task(
                    task_name="setup",
                    actual_task_name=actual_setup,
                    cwd=effective,
                    env_runner=repo_config.env_runner or self._config.env_runner,
                )
                continue

            # Normal repo: create its own worktree
            repo_path = repo_config.path

            if not self._git.is_ignored(repo_path, ".worktrees"):
                self._git.add_to_gitignore(repo_path, ".worktrees")

            wt_path = repo_path / ".worktrees" / branch
            self._git.worktree_add(
                repo_path=repo_path,
                worktree_path=wt_path,
                branch=branch,
            )
            worktrees[repo_name] = wt_path

            actual_setup = repo_config.tasks.get("setup", "setup")
            self._shell.run_task(
                task_name="setup",
                actual_task_name=actual_setup,
                cwd=wt_path,
                env_runner=repo_config.env_runner or self._config.env_runner,
            )

        task = Task(
            slug=slug,
            description=description,
            phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=ordered,
            worktrees=worktrees,
            branch=branch,
        )

        state = self._state_manager.load()
        state.tasks[slug] = task
        state.current_task = slug
        self._state_manager.save(state)

        self._log.create(slug)
        self._log.append(
            slug,
            f"Task spawned. Repos: {', '.join(ordered)}. Branch: {branch}",
        )

        return task

    def abort(self, task_slug: str) -> None:
        state = self._state_manager.load()
        task = state.tasks[task_slug]

        for repo_name, wt_path in task.worktrees.items():
            repo_config = self._config.repos[repo_name]

            # Skip git_root repos — their "worktree" is just a subdirectory
            # of the parent's worktree and will disappear with it
            if repo_config.git_root is not None:
                continue

            try:
                self._git.worktree_remove(
                    repo_path=repo_config.path,
                    worktree_path=Path(wt_path),
                )
            except Exception:
                import shutil
                shutil.rmtree(Path(wt_path), ignore_errors=True)
            try:
                self._git.branch_delete(
                    repo_path=repo_config.path,
                    branch=task.branch,
                )
            except Exception:
                pass

        # Only update state after all cleanup attempts
        del state.tasks[task_slug]
        if state.current_task == task_slug:
            state.current_task = None
        self._state_manager.save(state)

    def list_worktrees(self) -> dict[str, dict[str, Path]]:
        state = self._state_manager.load()
        return {
            slug: dict(task.worktrees)
            for slug, task in state.tasks.items()
        }

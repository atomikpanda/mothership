from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mship.core.config import WorkspaceConfig
from mship.core.graph import DependencyGraph
from mship.core.state import StateManager, TestResult
from mship.util.shell import ShellRunner, ShellResult


@dataclass
class RepoResult:
    repo: str
    task_name: str
    shell_result: ShellResult
    skipped: bool = False

    @property
    def success(self) -> bool:
        return self.shell_result.returncode == 0 if not self.skipped else True


@dataclass
class ExecutionResult:
    results: list[RepoResult] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return all(r.success for r in self.results)


class RepoExecutor:
    """Execute tasks across repos in dependency order."""

    def __init__(
        self,
        config: WorkspaceConfig,
        graph: DependencyGraph,
        state_manager: StateManager,
        shell: ShellRunner,
    ) -> None:
        self._config = config
        self._graph = graph
        self._state_manager = state_manager
        self._shell = shell

    def resolve_task_name(self, repo_name: str, canonical: str) -> str:
        repo = self._config.repos[repo_name]
        return repo.tasks.get(canonical, canonical)

    def resolve_env_runner(self, repo_name: str) -> str | None:
        repo = self._config.repos[repo_name]
        if repo.env_runner is not None:
            return repo.env_runner
        return self._config.env_runner

    def resolve_upstream_env(
        self, repo_name: str, task_slug: str | None
    ) -> dict[str, str]:
        """Compute UPSTREAM_* env vars for a repo's dependencies."""
        if task_slug is None:
            return {}
        state = self._state_manager.load()
        task = state.tasks.get(task_slug)
        if task is None or not task.worktrees:
            return {}

        env: dict[str, str] = {}
        repo_config = self._config.repos[repo_name]
        for dep in repo_config.depends_on:
            dep_name = dep.repo
            if dep_name in task.worktrees:
                var_name = f"UPSTREAM_{dep_name.upper().replace('-', '_')}"
                env[var_name] = str(task.worktrees[dep_name])
        return env

    def execute(
        self,
        canonical_task: str,
        repos: list[str],
        run_all: bool = False,
        task_slug: str | None = None,
    ) -> ExecutionResult:
        ordered = self._graph.topo_sort(repos)
        result = ExecutionResult()

        for repo_name in ordered:
            actual_name = self.resolve_task_name(repo_name, canonical_task)
            env_runner = self.resolve_env_runner(repo_name)
            repo_config = self._config.repos[repo_name]
            upstream_env = self.resolve_upstream_env(repo_name, task_slug)

            # Use worktree path if available, otherwise repo path
            cwd = repo_config.path
            if task_slug:
                state = self._state_manager.load()
                task = state.tasks.get(task_slug)
                if task and repo_name in task.worktrees:
                    wt_path = Path(task.worktrees[repo_name])
                    if wt_path.exists():
                        cwd = wt_path

            shell_result = self._shell.run_task(
                task_name=canonical_task,
                actual_task_name=actual_name,
                cwd=cwd,
                env_runner=env_runner,
                env=upstream_env or None,
            )

            repo_result = RepoResult(
                repo=repo_name,
                task_name=actual_name,
                shell_result=shell_result,
            )
            result.results.append(repo_result)

            if task_slug and canonical_task == "test":
                self._update_test_result(task_slug, repo_name, shell_result)

            if not repo_result.success and not run_all:
                break

        return result

    def _update_test_result(
        self, task_slug: str, repo_name: str, shell_result: ShellResult
    ) -> None:
        state = self._state_manager.load()
        task = state.tasks.get(task_slug)
        if task is None:
            return
        task.test_results[repo_name] = TestResult(
            status="pass" if shell_result.returncode == 0 else "fail",
            at=datetime.now(timezone.utc),
        )
        self._state_manager.save(state)

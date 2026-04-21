import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from mship.core.config import WorkspaceConfig
from mship.core.graph import DependencyGraph
from mship.core.log import LogManager
from mship.core.reconcile.fetch import workspace_default_branch_from_config
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.git import GitRunner
from mship.util.shell import ShellRunner
from mship.util.slug import slugify


@dataclass
class SpawnResult:
    task: Task
    setup_warnings: list[str] = field(default_factory=list)


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

    def _git_ignored_files(self, source_root: Path) -> list[PurePosixPath]:
        """Return gitignored leaf files in source_root, as relative PurePosixPath.

        Uses `git ls-files --others --ignored --exclude-standard`, which:
        - returns gitignored files at their relative paths,
        - does NOT descend into ignored directories (so .venv/*, node_modules/*, etc. are NOT listed),
        - returns ignored directories themselves as "dir/" entries, which we filter out.
        """
        result = self._shell.run(
            "git ls-files --others --ignored --exclude-standard",
            cwd=source_root,
        )
        if result.returncode != 0:
            return []
        out: list[PurePosixPath] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.endswith("/"):
                continue
            out.append(PurePosixPath(line))
        return out

    def _match_bind_patterns(
        self,
        patterns: list[str],
        candidates: list[PurePosixPath],
    ) -> list[PurePosixPath]:
        """Match patterns against candidate paths.

        Supports `*`, `?`, and `**` via pathlib's glob semantics. Dedups across
        patterns while preserving first-seen order.
        """
        seen: set[PurePosixPath] = set()
        out: list[PurePosixPath] = []
        for pattern in patterns:
            for cand in candidates:
                if cand in seen:
                    continue
                if cand.full_match(pattern):
                    seen.add(cand)
                    out.append(cand)
        return out

    def _copy_bind_files(
        self,
        repo_name: str,
        repo_config,
        worktree_path: Path,
    ) -> list[str]:
        """Copy bind_files matches from source repo into the worktree.

        Returns warnings (non-fatal). Matches `symlink_dirs`'s warnings style
        so spawn's existing warnings-surface handles display.
        """
        warnings: list[str] = []
        if not repo_config.bind_files:
            return warnings

        # Resolve source root (mirror _create_symlinks logic for git_root repos).
        if repo_config.git_root is not None:
            parent = self._config.repos[repo_config.git_root]
            source_root = parent.path / repo_config.path
        else:
            source_root = repo_config.path

        # Warn on missing literals (no glob chars) before running the enum.
        for entry in repo_config.bind_files:
            if any(c in entry for c in "*?["):
                continue   # it's a glob; zero-match handled silently below
            if not (source_root / entry).exists():
                warnings.append(
                    f"{repo_name}: bind_files source missing: {entry} (will not be copied)"
                )

        candidates = self._git_ignored_files(source_root)
        matches = self._match_bind_patterns(repo_config.bind_files, candidates)

        for rel in matches:
            src = source_root / rel
            dst = worktree_path / rel

            if not src.is_file():
                # Glob matched a directory or a broken symlink. Skip + warn.
                warnings.append(
                    f"{repo_name}: bind_files match is not a regular file: {rel} (skipped)"
                )
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        return warnings

    def _create_symlinks(
        self,
        repo_name: str,
        repo_config,
        worktree_path: Path,
    ) -> list[str]:
        """Create symlinks from source repo into the worktree. Returns warnings."""
        warnings: list[str] = []
        if not repo_config.symlink_dirs:
            return warnings

        if repo_config.git_root is not None:
            parent = self._config.repos[repo_config.git_root]
            source_root = parent.path / repo_config.path
        else:
            source_root = repo_config.path

        for dir_name in repo_config.symlink_dirs:
            source = source_root / dir_name
            target = worktree_path / dir_name

            if not source.exists():
                warnings.append(
                    f"{repo_name}: symlink source missing: {dir_name} (will not be linked)"
                )
                continue

            if target.exists() and not target.is_symlink():
                warnings.append(
                    f"{repo_name}: symlink skipped, {dir_name} already exists as a real directory"
                )
                continue

            if target.is_symlink():
                target.unlink()

            target.symlink_to(source.resolve())

        return warnings

    def spawn(
        self,
        description: str,
        repos: list[str] | None = None,
        skip_setup: bool = False,
        slug: str | None = None,
    ) -> SpawnResult:
        slug = slug if slug is not None else slugify(description)
        branch = self._config.branch_pattern.replace("{slug}", slug)

        # Early-exit preflight: avoid doing expensive worktree creation when
        # the slug is already registered. This is an OPTIMIZATION ONLY — the
        # authoritative check-and-set happens inside the mutate() below so
        # two concurrent spawns with the same slug cannot both race past this
        # check and both register the task.
        state = self._state_manager.load()
        if slug in state.tasks:
            raise ValueError(
                f"Task '{slug}' already exists. "
                f"Run `mship close --yes --abandon --task {slug}` to remove it first, or use a different description."
            )

        if repos is None:
            repos = list(self._config.repos.keys())

        ordered = self._graph.topo_sort(repos)

        worktrees: dict[str, Path] = {}
        setup_warnings: list[str] = []

        for repo_name in ordered:
            repo_config = self._config.repos[repo_name]

            if repo_config.git_root is not None:
                # Subdirectory service: share parent's worktree
                parent_wt = worktrees.get(repo_config.git_root)
                if parent_wt is None:
                    parent_wt = self._config.repos[repo_config.git_root].path
                effective = parent_wt / repo_config.path
                worktrees[repo_name] = effective

                # Create symlinks before setup so setup can use the linked dirs
                symlink_warnings = self._create_symlinks(repo_name, repo_config, effective)
                setup_warnings.extend(symlink_warnings)
                bind_warnings = self._copy_bind_files(repo_name, repo_config, effective)
                setup_warnings.extend(bind_warnings)

                if not skip_setup and shutil.which("task") is not None:
                    actual_setup = repo_config.tasks.get("setup", "setup")
                    setup_result = self._shell.run_task(
                        task_name="setup",
                        actual_task_name=actual_setup,
                        cwd=effective,
                        env_runner=repo_config.env_runner or self._config.env_runner,
                    )
                    if setup_result.returncode != 0:
                        setup_warnings.append(
                            f"{repo_name}: setup failed (task '{actual_setup}') — "
                            f"{setup_result.stderr.strip()[:200]}"
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

            # Create symlinks before setup so setup can use the linked dirs
            symlink_warnings = self._create_symlinks(repo_name, repo_config, wt_path)
            setup_warnings.extend(symlink_warnings)
            bind_warnings = self._copy_bind_files(repo_name, repo_config, wt_path)
            setup_warnings.extend(bind_warnings)

            if not skip_setup and shutil.which("task") is not None:
                actual_setup = repo_config.tasks.get("setup", "setup")
                setup_result = self._shell.run_task(
                    task_name="setup",
                    actual_task_name=actual_setup,
                    cwd=wt_path,
                    env_runner=repo_config.env_runner or self._config.env_runner,
                )
                if setup_result.returncode != 0:
                    setup_warnings.append(
                        f"{repo_name}: setup failed (task '{actual_setup}') — "
                        f"{setup_result.stderr.strip()[:200]}"
                    )

        base_branch = workspace_default_branch_from_config(self._config) or "main"
        task = Task(
            slug=slug,
            description=description,
            phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=ordered,
            worktrees=worktrees,
            branch=branch,
            base_branch=base_branch,
        )

        def _apply(s: WorkspaceState) -> None:
            # Atomic check-and-set: re-check under the exclusive lock so two
            # concurrent spawns with the same slug cannot both register. The
            # caller-facing error matches the preflight message.
            if slug in s.tasks:
                raise ValueError(
                    f"Task '{slug}' already exists. "
                    f"Run `mship close --yes --abandon --task {slug}` to remove it first, or use a different description."
                )
            s.tasks[slug] = task
        self._state_manager.mutate(_apply)

        self._log.create(slug)
        log_msg = f"Task spawned. Repos: {', '.join(ordered)}. Branch: {branch}"
        if skip_setup:
            log_msg += " (setup skipped)"
        self._log.append(slug, log_msg)

        return SpawnResult(task=task, setup_warnings=setup_warnings)

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
        def _abort(s):
            s.tasks.pop(task_slug, None)
        self._state_manager.mutate(_abort)

    def list_worktrees(self) -> dict[str, dict[str, Path]]:
        state = self._state_manager.load()
        return {
            slug: dict(task.worktrees)
            for slug, task in state.tasks.items()
        }

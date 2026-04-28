import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from mship.core.config import WorkspaceConfig
from mship.core.graph import DependencyGraph
from mship.core.log import LogManager
from mship.core.reconcile.fetch import workspace_default_branch_from_config
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.workspace_marker import write_marker
from mship.util.git import GitRunner
from mship.util.shell import ShellRunner
from mship.util.slug import slugify


@dataclass
class SpawnResult:
    task: Task
    setup_warnings: list[str] = field(default_factory=list)


def _symlink_gitignore_footgun(repo_path: Path, name: str) -> bool:
    """Return True when `.gitignore` ignores `<name>/` (dir form) but not `<name>` alone.

    This is the specific footgun that breaks `symlink_dirs`: git treats the
    symlink as a file, not a directory, so a dir-only ignore pattern
    (`foo/`) doesn't match the symlink (`foo`), and it shows up as untracked.

    Probes via `git check-ignore` — exit 0 = ignored, 1 = not ignored, >1 = error.

    Post-symlink wrinkle: once `<name>` is a symlink pointing outside the
    repo, `git check-ignore <name>/` fails with `fatal: pathspec is beyond
    a symbolic link` (exit 128). Fall back to probing under a synthetic
    non-existent parent (`_mship_probe_absent_/<name>/`) to force pure
    pattern matching. This loses anchored patterns like `/foo/` in the
    post-symlink case — but spawn catches those via the direct probe
    before the symlink is created, so the common unanchored case is
    covered from both call sites.

    On any error we bail to False (no warning) to avoid false positives.
    """
    def _probe(path_fragment: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", "check-ignore", "--", path_fragment],
                cwd=repo_path, capture_output=True, text=True, check=False,
            )
        except OSError:
            return None

    dir_r = _probe(f"{name}/")
    file_r = _probe(name)
    if dir_r is None or file_r is None:
        return False

    # Fallback for post-symlink case: probe via non-existent parent to force
    # pure pattern matching when the direct dir-form probe hits the
    # "beyond a symbolic link" error.
    if (
        dir_r.returncode == 128
        and "beyond a symbolic link" in dir_r.stderr.lower()
    ):
        dir_r = _probe(f"_mship_probe_absent_/{name}/")
        if dir_r is None:
            return False

    dir_ignored = dir_r.returncode == 0
    file_ignored = file_r.returncode == 0
    return dir_ignored and not file_ignored


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

    def refresh_bind_files(
        self,
        repo_name: str,
        repo_config,
        worktree_path: Path,
        overwrite: bool = False,
    ) -> dict:
        """Re-sync bind_files from source into an existing worktree. See #71.

        Returns a dict with keys:
        - `copied`:    files that didn't exist in worktree and were copied
        - `updated`:   files that differed and were overwritten (only when overwrite=True)
        - `unchanged`: files that already matched source byte-for-byte
        - `skipped`:   files that differed but were preserved (overwrite=False)
        - `warnings`:  missing-source / not-a-regular-file warnings (same shape as _copy_bind_files)

        All lists contain relative path strings. Caller decides exit status
        based on whether `skipped` is non-empty.
        """
        result: dict[str, list] = {
            "copied": [], "updated": [], "unchanged": [], "skipped": [],
            "warnings": [],
        }
        if not repo_config.bind_files:
            return result

        if repo_config.git_root is not None:
            parent = self._config.repos[repo_config.git_root]
            source_root = parent.path / repo_config.path
        else:
            source_root = repo_config.path

        for entry in repo_config.bind_files:
            if any(c in entry for c in "*?["):
                continue
            if not (source_root / entry).exists():
                result["warnings"].append(
                    f"{repo_name}: bind_files source missing: {entry} (will not be copied)"
                )

        candidates = self._git_ignored_files(source_root)
        matches = self._match_bind_patterns(repo_config.bind_files, candidates)

        for rel in matches:
            src = source_root / rel
            dst = worktree_path / rel
            rel_str = str(rel)

            if not src.is_file():
                result["warnings"].append(
                    f"{repo_name}: bind_files match is not a regular file: {rel} (skipped)"
                )
                continue

            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                result["copied"].append(rel_str)
                continue

            # dst exists — compare bytes.
            if src.read_bytes() == dst.read_bytes():
                result["unchanged"].append(rel_str)
                continue

            if overwrite:
                shutil.copy2(src, dst)
                result["updated"].append(rel_str)
            else:
                result["skipped"].append(rel_str)

        return result

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

    def refresh_symlink_dirs(
        self,
        repo_name: str,
        repo_config,
        worktree_path: Path,
        overwrite: bool = False,
    ) -> dict:
        """Re-evaluate symlink_dirs in an existing worktree. See #111.

        Returns a dict with keys (mirroring `refresh_bind_files`):
        - `copied`:    symlinks that didn't exist and were created
        - `updated`:   symlinks pointing at the wrong target, replaced (overwrite=True only)
        - `unchanged`: symlinks already pointing at the correct target
        - `skipped`:   symlinks pointing elsewhere preserved (overwrite=False),
                      OR a real directory exists at the target (always preserved)
        - `warnings`:  missing-source / footgun warnings

        Real directories at the target are NEVER overwritten, even with
        overwrite=True — that would risk destroying user data.
        """
        result: dict[str, list] = {
            "copied": [], "updated": [], "unchanged": [], "skipped": [],
            "warnings": [],
        }
        if not repo_config.symlink_dirs:
            return result

        if repo_config.git_root is not None:
            parent = self._config.repos[repo_config.git_root]
            source_root = parent.path / repo_config.path
        else:
            source_root = repo_config.path

        for dir_name in repo_config.symlink_dirs:
            source = source_root / dir_name
            target = worktree_path / dir_name
            expected = source.resolve() if source.exists() else None

            if expected is None:
                result["warnings"].append(
                    f"{repo_name}: symlink source missing: {dir_name} (will not be linked)"
                )
                continue

            if target.exists() and not target.is_symlink():
                # Real directory in the worktree — never destroyed.
                result["skipped"].append(dir_name)
                result["warnings"].append(
                    f"{repo_name}: {dir_name} is a real directory in the worktree — "
                    f"refusing to replace with symlink (preserve user data)."
                )
                continue

            if target.is_symlink():
                try:
                    current = target.resolve()
                except OSError:
                    current = None
                if current == expected:
                    result["unchanged"].append(dir_name)
                    continue
                if overwrite:
                    target.unlink()
                    target.symlink_to(expected)
                    result["updated"].append(dir_name)
                else:
                    result["skipped"].append(dir_name)
                continue

            # Nothing at target — create the symlink.
            footgun = _symlink_gitignore_footgun(worktree_path, dir_name)
            target.symlink_to(expected)
            result["copied"].append(dir_name)
            if footgun:
                result["warnings"].append(
                    f"{repo_name}: symlink {dir_name!r} is not ignored — "
                    f"git treats it as an untracked file. "
                    f"Add '{dir_name}' (not just '{dir_name}/') to .gitignore."
                )

        return result

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

            # Detect the `.gitignore has 'foo/' but not 'foo'` footgun before
            # creating the symlink — git check-ignore behaves differently once
            # the symlink exists (exit 128 for 'foo/' when foo → external dir).
            # Probe while the path is still absent from the worktree. See #72.
            footgun = _symlink_gitignore_footgun(worktree_path, dir_name)

            if target.is_symlink():
                target.unlink()

            target.symlink_to(source.resolve())

            if footgun:
                warnings.append(
                    f"{repo_name}: symlink '{dir_name}' is not ignored — "
                    f"git treats it as an untracked file. "
                    f"Add '{dir_name}' (not just '{dir_name}/') to .gitignore."
                )

        return warnings

    def spawn(
        self,
        description: str,
        repos: list[str] | None = None,
        skip_setup: bool = False,
        slug: str | None = None,
        workspace_root: Path | None = None,
        offline: bool = False,
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

        # Passive expansion: collect transitive depends_on of each repo in
        # `ordered` that isn't already in `ordered`. Topo-sort the union so
        # passive deps materialize before their consumers.
        affected = set(ordered)
        passive: set[str] = set()
        frontier = list(ordered)
        while frontier:
            r = frontier.pop()
            for dep in self._graph.direct_deps(r):
                if dep not in affected and dep not in passive:
                    passive.add(dep)
                    frontier.append(dep)
        all_repos = self._graph.topo_sort(list(affected | passive))

        worktrees: dict[str, Path] = {}
        setup_warnings: list[str] = []

        if workspace_root is None:
            raise ValueError(
                "workspace_root required for hub layout spawn; "
                "callers must pass container.config_path().parent"
            )

        hub = workspace_root / ".worktrees" / slug
        hub.mkdir(parents=True, exist_ok=True)

        # Workspace-root .gitignore gets .worktrees if root is a git repo.
        if (workspace_root / ".git").exists():
            if not self._git.is_ignored(workspace_root, ".worktrees"):
                self._git.add_to_gitignore(workspace_root, ".worktrees")

        for repo_name in all_repos:
            repo_config = self._config.repos[repo_name]
            is_passive = repo_name in passive

            if repo_config.git_root is not None:
                # Subdirectory child: nested inside parent's hub worktree.
                parent_wt = worktrees.get(repo_config.git_root)
                if parent_wt is None:
                    parent_wt = self._config.repos[repo_config.git_root].path
                effective = parent_wt / repo_config.path
                worktrees[repo_name] = effective

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

            # Normal repo: hub worktree.
            repo_path = repo_config.path
            wt_path = hub / repo_name

            if is_passive:
                # Passive: detached HEAD at origin/<expected || base>.
                ref = repo_config.expected_branch or repo_config.base_branch
                if ref is None:
                    raise ValueError(
                        f"Passive materialization for '{repo_name}' requires "
                        f"`expected_branch` or `base_branch` declared in "
                        f"mothership.yaml."
                    )
                if not offline:
                    fetched = self._git.fetch_remote_ref(repo_path=repo_path, ref=ref)
                    if not fetched:
                        raise RuntimeError(
                            f"Failed to fetch origin/{ref} for passive repo "
                            f"'{repo_name}'. Re-run with `--offline` to use "
                            f"the local ref."
                        )
                    target_ref = f"origin/{ref}"
                else:
                    target_ref = ref
                self._git.worktree_add_detached(
                    repo_path=repo_path,
                    worktree_path=wt_path,
                    ref=target_ref,
                )
            else:
                self._git.worktree_add(
                    repo_path=repo_path,
                    worktree_path=wt_path,
                    branch=branch,
                )
            worktrees[repo_name] = wt_path

            symlink_warnings = self._create_symlinks(repo_name, repo_config, wt_path)
            setup_warnings.extend(symlink_warnings)
            bind_warnings = self._copy_bind_files(repo_name, repo_config, wt_path)
            setup_warnings.extend(bind_warnings)

            # Skip task setup for passive worktrees.
            if not is_passive and not skip_setup and shutil.which("task") is not None:
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

        # Single .mship-workspace marker at the hub root.
        write_marker(hub, workspace_root)

        task = Task(
            slug=slug,
            description=description,
            phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=ordered,
            worktrees=worktrees,
            branch=branch,
            base_branch=workspace_default_branch_from_config(self._config),
            passive_repos=passive,
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

        # Remove the hub directory for this task. Inferred from the first
        # worktree's parent.parent, which is `<workspace>/.worktrees/<slug>/`.
        # Legacy per-repo-layout tasks have a different parent shape — leave
        # those alone.
        try:
            sample_wt = None
            for name, wt in task.worktrees.items():
                if self._config.repos[name].git_root is None:
                    sample_wt = wt
                    break
            if sample_wt is not None:
                hub = Path(sample_wt).parent
                # Sanity: only remove if it looks like a hub (parent ends in .worktrees)
                if hub.name == task_slug and hub.parent.name == ".worktrees":
                    if hub.exists():
                        import shutil as _shutil
                        _shutil.rmtree(hub, ignore_errors=True)
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

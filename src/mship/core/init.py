from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from mship.core.config import RepoConfig, WorkspaceConfig, resolve_go_task_files


REPO_MARKERS = [
    ".git",
    "Taskfile.yml",
    "package.json",
    "go.mod",
    "pyproject.toml",
    "Cargo.toml",
    "build.gradle",
    "pom.xml",
]

SKIP_DIRS = {
    "node_modules",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
}


@dataclass
class DetectedRepo:
    path: Path
    markers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TaskfileWriteResult:
    """Outcome of WorkspaceInitializer.write_taskfile.

    - wrote:    a fresh Taskfile.yml stub was written (dir had no go-task file).
    - existing: the go-task file that suppressed the stub (None when wrote)."""
    wrote: bool
    existing: Path | None = None

    @property
    def needs_rename(self) -> bool:
        """True when the existing go-task file is a non-`Taskfile.yml` spelling
        that go-task resolves but mship's stub-based tooling keys off `.yml`."""
        return self.existing is not None and self.existing.name != "Taskfile.yml"


class WorkspaceInitializer:
    """Detects repos, generates config, and scaffolds files."""

    def detect_repos(self, workspace_path: Path) -> list[DetectedRepo]:
        repos: list[DetectedRepo] = []

        # Check current directory itself
        current_markers = self._find_markers(workspace_path)
        if current_markers:
            repos.append(DetectedRepo(path=workspace_path, markers=current_markers))

        # Check immediate subdirectories
        for item in sorted(workspace_path.iterdir()):
            if not item.is_dir():
                continue
            if item.name.startswith("."):
                continue
            if item.name in SKIP_DIRS:
                continue
            markers = self._find_markers(item)
            if markers:
                repos.append(DetectedRepo(path=item, markers=markers))

        return repos

    def plan_detected_repos(
        self, workspace_path: Path, detected: list[DetectedRepo]
    ) -> list[dict]:
        """Classify detected repos into config-entry dicts with RELATIVE paths
        and, for non-git subdirs of a git-owning root, a `git_root` back-ref.

        Rules (spec mship-init-detect-monorepo / issue #366 finding #4):
        - The workspace root (path == workspace_path), if detected, is emitted
          standalone with `path: '.'` and no git_root.
        - A subdir owning its own `.git` (a `.git` dir OR a submodule gitlink
          `.git` file — both make `_find_markers` record ".git") stays standalone
          with a path relative to the root and no git_root (ac3).
        - A subdir with NO `.git`, when the root IS a git owner, becomes a
          `git_root: <root-name>` child with a path relative to the root (ac1).
          Single-level detection: the parent IS the root, so relative-to-root
          equals the `(parent.path / child.path)` resolution contract.
        - If the root is not a git owner, non-git subdirs fall back to standalone
          emission — never point git_root at a non-git root (ac8).
        All emitted paths are relative for portability (ac2).
        """
        root_repo = next(
            (d for d in detected if d.path == workspace_path), None
        )
        root_is_git_owner = root_repo is not None and ".git" in root_repo.markers
        root_name = workspace_path.name

        entries: list[dict] = []
        for d in detected:
            if d.path == workspace_path:
                entries.append({
                    "name": root_name,
                    "path": ".",
                    "type": "service",
                    "git_root": None,
                    "depends_on": [],
                })
                continue
            rel = d.path.relative_to(workspace_path)
            has_own_git = ".git" in d.markers
            git_root = None if (has_own_git or not root_is_git_owner) else root_name
            entries.append({
                "name": d.path.name,
                "path": str(rel),
                "type": "service",
                "git_root": git_root,
                "depends_on": [],
            })
        return entries

    def _find_markers(self, path: Path) -> list[str]:
        markers: list[str] = []
        for marker in REPO_MARKERS:
            target = path / marker
            if not target.exists():
                continue
            if marker == "Taskfile.yml" and self._is_generated_stub(target):
                # A dir whose only marker is mship's OWN generated stub Taskfile
                # must not be re-promoted to a repo on a later `init --detect`.
                # Match by content (not filename) so a hand-written Taskfile.yml
                # still counts. See issue #366 finding #1.
                continue
            markers.append(marker)
        return markers

    def _is_generated_stub(self, taskfile: Path) -> bool:
        try:
            return taskfile.read_text() == self.TASKFILE_TEMPLATE
        except OSError:
            return False

    TASKFILE_TEMPLATE = """\
version: '3'

# Starter stub generated by `mship init`. Every task FAILS on purpose (exit 1)
# so an unedited stub can never fabricate a passing `mship test`. Replace each
# `exit 1` with the real command for this repo. See issue 366 finding 1.
tasks:
  test:
    desc: Run tests (stub - replace exit 1 with your test command)
    cmds:
      - exit 1

  run:
    desc: Start the service (stub - replace exit 1 with your run command)
    cmds:
      - exit 1

  lint:
    desc: Run linter (stub - replace exit 1 with your lint command)
    cmds:
      - exit 1

  setup:
    desc: Set up development environment (stub - replace exit 1 with your setup command)
    cmds:
      - exit 1
"""

    def generate_config(
        self,
        workspace_name: str,
        repos: list[dict],
        env_runner: str | None,
    ) -> WorkspaceConfig:
        """Build and validate a WorkspaceConfig from user inputs."""
        repo_configs: dict[str, RepoConfig] = {}
        for repo in repos:
            repo_configs[repo["name"]] = RepoConfig(
                path=Path(repo["path"]),
                type=repo["type"],
                depends_on=repo.get("depends_on", []),
                git_root=repo.get("git_root"),
            )

        config = WorkspaceConfig(
            workspace=workspace_name,
            env_runner=env_runner,
            repos=repo_configs,
        )
        return config

    def write_config(self, path: Path, config: WorkspaceConfig) -> None:
        """Write mothership.yaml."""
        data: dict = {
            "workspace": config.workspace,
        }
        if config.env_runner:
            data["env_runner"] = config.env_runner

        repos_data: dict = {}
        for name, repo in config.repos.items():
            repo_data: dict = {
                "path": str(repo.path),
                "type": repo.type,
            }
            if repo.git_root is not None:
                repo_data["git_root"] = repo.git_root
            if repo.depends_on:
                serialized_deps = []
                for dep in repo.depends_on:
                    if dep.type == "compile":
                        serialized_deps.append(dep.repo)
                    else:
                        serialized_deps.append({"repo": dep.repo, "type": dep.type})
                repo_data["depends_on"] = serialized_deps
            repos_data[name] = repo_data
        data["repos"] = repos_data

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def write_taskfile(self, repo_path: Path) -> TaskfileWriteResult:
        """Write a starter Taskfile.yml only when NO go-task file already resolves
        in `repo_path`. Returns a result describing what happened so callers can
        offer a rename for a non-`.yml` spelling instead of shadowing it with a
        generated `Taskfile.yml` stub. See issue #366 finding #1."""
        existing = resolve_go_task_files(repo_path)
        if existing:
            return TaskfileWriteResult(wrote=False, existing=existing[0])
        (repo_path / "Taskfile.yml").write_text(self.TASKFILE_TEMPLATE)
        return TaskfileWriteResult(wrote=True, existing=None)

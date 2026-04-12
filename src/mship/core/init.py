from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from mship.core.config import RepoConfig, WorkspaceConfig


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

    def _find_markers(self, path: Path) -> list[str]:
        markers: list[str] = []
        for marker in REPO_MARKERS:
            if (path / marker).exists():
                markers.append(marker)
        return markers

    TASKFILE_TEMPLATE = """\
version: '3'

tasks:
  test:
    desc: Run tests
    cmds:
      - echo "TODO - add test command"

  run:
    desc: Start the service
    cmds:
      - echo "TODO - add run command"

  lint:
    desc: Run linter
    cmds:
      - echo "TODO - add lint command"

  setup:
    desc: Set up development environment
    cmds:
      - echo "TODO - add setup command"
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

    def write_taskfile(self, repo_path: Path) -> None:
        """Write a starter Taskfile.yml if one doesn't exist."""
        taskfile = repo_path / "Taskfile.yml"
        if taskfile.exists():
            return
        taskfile.write_text(self.TASKFILE_TEMPLATE)

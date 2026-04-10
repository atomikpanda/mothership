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

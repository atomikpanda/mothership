from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator


class RepoConfig(BaseModel):
    path: Path
    type: Literal["library", "service"]
    depends_on: list[str] = []
    env_runner: str | None = None
    tasks: dict[str, str] = {}


class WorkspaceConfig(BaseModel):
    workspace: str
    env_runner: str | None = None
    branch_pattern: str = "feat/{slug}"
    repos: dict[str, RepoConfig]

    @model_validator(mode="after")
    def validate_depends_on_refs(self) -> "WorkspaceConfig":
        repo_names = set(self.repos.keys())
        for name, repo in self.repos.items():
            for dep in repo.depends_on:
                if dep not in repo_names:
                    raise ValueError(
                        f"Repo '{name}' depends on '{dep}' which does not exist. "
                        f"Valid repos: {sorted(repo_names)}"
                    )
        return self

    @model_validator(mode="after")
    def validate_no_cycles(self) -> "WorkspaceConfig":
        # Kahn's algorithm for cycle detection
        in_degree: dict[str, int] = {name: 0 for name in self.repos}
        adjacency: dict[str, list[str]] = {name: [] for name in self.repos}
        for name, repo in self.repos.items():
            for dep in repo.depends_on:
                adjacency[dep].append(name)
                in_degree[name] += 1

        queue = [name for name, degree in in_degree.items() if degree == 0]
        visited = 0
        while queue:
            node = queue.pop(0)
            visited += 1
            for neighbor in adjacency[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited != len(self.repos):
            raise ValueError("Circular dependency detected in repo graph")
        return self


class ConfigLoader:
    """Loads and validates mothership.yaml."""

    @staticmethod
    def load(path: Path) -> WorkspaceConfig:
        with open(path) as f:
            raw = yaml.safe_load(f)

        workspace_root = path.parent

        config = WorkspaceConfig(**raw)

        # Resolve relative paths and validate directories
        for name, repo in config.repos.items():
            resolved = (workspace_root / repo.path).resolve()
            repo.path = resolved
            if not resolved.is_dir():
                raise ValueError(f"Repo '{name}' path does not exist: {resolved}")
            if not (resolved / "Taskfile.yml").exists():
                raise ValueError(
                    f"Repo '{name}' at {resolved} has no Taskfile.yml"
                )

        return config

    @staticmethod
    def discover(start: Path) -> Path:
        current = start.resolve()
        while True:
            candidate = current / "mothership.yaml"
            if candidate.exists():
                return candidate
            parent = current.parent
            if parent == current:
                raise FileNotFoundError(
                    "No mothership.yaml found in any parent directory"
                )
            current = parent

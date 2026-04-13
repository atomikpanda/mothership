from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator


class Dependency(BaseModel):
    repo: str
    type: Literal["compile", "runtime"] = "compile"


class Healthcheck(BaseModel):
    tcp: str | None = None
    http: str | None = None
    sleep: str | None = None
    task: str | None = None
    timeout: str = "30s"
    retry_interval: str = "500ms"

    @model_validator(mode="after")
    def exactly_one_probe(self) -> "Healthcheck":
        probes = [self.tcp, self.http, self.sleep, self.task]
        set_count = sum(1 for p in probes if p is not None)
        if set_count != 1:
            raise ValueError(
                "healthcheck must specify exactly one of: tcp, http, sleep, task"
            )
        return self


class RepoConfig(BaseModel):
    path: Path
    type: Literal["library", "service"]
    depends_on: list[Dependency] = []
    env_runner: str | None = None
    tasks: dict[str, str] = {}
    tags: list[str] = []
    git_root: str | None = None
    start_mode: Literal["foreground", "background"] = "foreground"
    symlink_dirs: list[str] = []
    healthcheck: Healthcheck | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_depends_on(cls, data):
        """Normalize string depends_on entries to Dependency objects."""
        if isinstance(data, dict) and "depends_on" in data:
            normalized = []
            for dep in data["depends_on"]:
                if isinstance(dep, str):
                    normalized.append({"repo": dep, "type": "compile"})
                else:
                    normalized.append(dep)
            data["depends_on"] = normalized
        return data


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
                if dep.repo not in repo_names:
                    raise ValueError(
                        f"Repo '{name}' depends on '{dep.repo}' which does not exist. "
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
                adjacency[dep.repo].append(name)
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

    @model_validator(mode="after")
    def validate_git_root_refs(self) -> "WorkspaceConfig":
        repo_names = set(self.repos.keys())
        for name, repo in self.repos.items():
            if repo.git_root is None:
                continue
            if repo.git_root not in repo_names:
                raise ValueError(
                    f"Repo '{name}' has git_root '{repo.git_root}' which does not exist. "
                    f"Valid repos: {sorted(repo_names)}"
                )
            # No chaining: the referenced repo cannot itself have git_root set
            parent = self.repos[repo.git_root]
            if parent.git_root is not None:
                raise ValueError(
                    f"Repo '{name}' git_root '{repo.git_root}' is itself a subdirectory service. "
                    f"Cannot chain git_root references."
                )
        return self


class ConfigLoader:
    """Loads and validates mothership.yaml."""

    @staticmethod
    def load(path: Path) -> WorkspaceConfig:
        with open(path) as f:
            raw = yaml.safe_load(f)

        workspace_root = path.parent

        config = WorkspaceConfig(**raw)

        # First pass: resolve paths and validate for repos WITHOUT git_root
        for name, repo in config.repos.items():
            if repo.git_root is not None:
                continue
            resolved = (workspace_root / repo.path).resolve()
            repo.path = resolved
            if not resolved.is_dir():
                raise ValueError(f"Repo '{name}' path does not exist: {resolved}")
            if not (resolved / "Taskfile.yml").exists():
                raise ValueError(
                    f"Repo '{name}' at {resolved} has no Taskfile.yml"
                )

        # Second pass: validate git_root repos against their parent's resolved path
        for name, repo in config.repos.items():
            if repo.git_root is None:
                continue
            parent = config.repos[repo.git_root]
            effective = (parent.path / repo.path).resolve()
            if not effective.is_dir():
                raise ValueError(
                    f"Repo '{name}' subdirectory does not exist: {effective}"
                )
            if not (effective / "Taskfile.yml").exists():
                raise ValueError(
                    f"Repo '{name}' at {effective} has no Taskfile.yml"
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

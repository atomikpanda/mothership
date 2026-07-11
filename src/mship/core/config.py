from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator, model_validator

from mship.core.relay.config import RelayConfig


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


class CaptureConfig(BaseModel):
    platforms: list[str] = []


# Lifecycle-hook event catalog (v1) — see spec mship-lifecycle-hooks (MOS-220).
# Task phases mirror core/phase.py's `Phase` Literal (plan/dev/review/run);
# WorkItem phases mirror core/workitem.py's `Phase` Literal (inbox/shaping/
# ready/in_flight/review/done) — a distinct, unrelated enum, kept as its own
# event family rather than one shared `phase.entered.*`. If either source
# enum changes, update the corresponding set below too.
_TASK_PHASE_EVENTS = frozenset(
    f"phase.entered.{p}" for p in ("plan", "dev", "review", "run")
)
_WORKITEM_PHASE_EVENTS = frozenset(
    f"workitem.phase.{p}"
    for p in ("inbox", "shaping", "ready", "in_flight", "review", "done")
)
LIFECYCLE_EVENTS: frozenset[str] = frozenset(
    {"task.finished", "task.closed", "pr.merged", "pr.closed"}
    | _TASK_PHASE_EVENTS
    | _WORKITEM_PHASE_EVENTS
)

# `pr.merged`/`pr.closed` are polling-derived (PrWatcher's sweep cadence) —
# by the time the transition is observed it has already happened, so
# `required: true` can't block anything there. Rejected at config-load time
# rather than silently accepted-but-meaningless. See spec open question q5.
_NON_BLOCKING_EVENTS: frozenset[str] = frozenset({"pr.merged", "pr.closed"})


class HookConfig(BaseModel):
    """One `hooks:` entry: run a go-task target or shell command when `on`
    fires. See mship.core.lifecycle_hooks for the runtime dispatcher — NOT
    mship.core.hooks, which is the unrelated git pre-commit/pre-push installer."""
    on: str
    run: str
    repo: str | None = None
    name: str | None = None
    timeout: int | None = None
    required: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_on_key(cls, data):
        """PyYAML's SafeLoader parses YAML 1.1, which treats a bare `on` (or
        `off`/`yes`/`no`) key as a boolean rather than a string — so `on:
        pr.merged` in mothership.yaml round-trips through yaml.safe_load as
        `{True: 'pr.merged', ...}`, not `{'on': 'pr.merged', ...}`. Normalize
        before field validation so the config keeps reading naturally as
        `on:` in the yaml source."""
        if isinstance(data, dict) and True in data and "on" not in data:
            data = dict(data)
            data["on"] = data.pop(True)
        return data

    @field_validator("on", mode="after")
    @classmethod
    def validate_on(cls, v: str) -> str:
        if v not in LIFECYCLE_EVENTS:
            raise ValueError(
                f"hooks: unknown event {v!r}. Valid events: "
                f"{', '.join(sorted(LIFECYCLE_EVENTS))}"
            )
        return v

    @field_validator("run", mode="after")
    @classmethod
    def validate_run(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("hooks: `run` must be a non-empty string")
        return v

    @model_validator(mode="after")
    def validate_required_not_on_polling_events(self) -> "HookConfig":
        if self.required and self.on in _NON_BLOCKING_EVENTS:
            raise ValueError(
                f"hooks: `required: true` is not meaningful on {self.on!r} — "
                f"pr.merged/pr.closed are detected after the fact by "
                f"PrWatcher's poll, so a hook here cannot block a transition "
                f"that already happened. Remove `required: true` for this hook."
            )
        return self


class RepoConfig(BaseModel):
    path: Path
    type: Literal["library", "service"]
    depends_on: list[Dependency] = []
    env_runner: str | None = None
    tasks: dict[str, str] = {}
    not_applicable: list[str] = []
    tags: list[str] = []
    git_root: str | None = None
    start_mode: Literal["foreground", "background"] = "foreground"
    symlink_dirs: list[str] = []
    bind_files: list[str] = []
    healthcheck: Healthcheck | None = None
    base_branch: str | None = None
    expected_branch: str | None = None
    url: str | None = None
    allow_dirty: bool = False
    allow_extra_worktrees: bool = False
    capture: CaptureConfig | None = None
    # Logical run-host role this repo uses for `--remote` execution (e.g. an
    # iOS simulator or Android emulator machine). Must name an entry declared
    # in the workspace's `run_hosts:` list; the concrete {url, token} for the
    # role lives in the gitignored `.mothership/run-hosts.yaml` store, never
    # here. See mship.core.run_host.resolve_run_host.
    run_host: str | None = None

    @field_validator("url", mode="after")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        stripped = v.strip()
        if not stripped:
            raise ValueError("url must be a non-empty string when provided")
        # Normalize at parse time so any reader of repo.url sees the trimmed value.
        return stripped

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

    @model_validator(mode="after")
    def validate_not_applicable(self) -> "RepoConfig":
        """A canonical task can't be both declared (tasks: ...) and declared
        not applicable — those are contradictory intents. See #76."""
        overlap = set(self.tasks.keys()) & set(self.not_applicable)
        if overlap:
            raise ValueError(
                f"tasks and not_applicable overlap on {sorted(overlap)}; "
                f"a task is either declared (in `tasks`) or explicitly "
                f"not applicable, not both"
            )
        return self

    @model_validator(mode="after")
    def validate_bind_files(self) -> "RepoConfig":
        for entry in self.bind_files:
            p = Path(entry)
            if p.is_absolute():
                raise ValueError(
                    f"bind_files entry {entry!r} is absolute; bind_files must be relative paths or globs"
                )
            if ".." in p.parts:
                raise ValueError(
                    f"bind_files entry {entry!r} contains '..'; bind_files must stay inside the repo"
                )
        return self


class AuditPolicy(BaseModel):
    block_spawn: bool = True
    block_finish: bool = True


class WorkspaceConfig(BaseModel):
    workspace: str
    env_runner: str | None = None
    branch_pattern: str = "feat/{slug}"
    audit: AuditPolicy = AuditPolicy()
    # Default repo scope when `mship spawn` is invoked without --repos.
    # - "all" (default): use every repo — today's behavior.
    # - "none": require explicit --repos; no-flag spawn errors with the repo list.
    # - list[str]: use those repos as the default. See #74.
    default_scope: str | list[str] = "all"
    # If set and the effective spawn scope exceeds N repos AND no --repos was
    # passed, require confirmation (TTY) or --yes (non-TTY). See #74.
    spawn_confirm_threshold: int | None = None
    # Workspace-relative paths searched for specs by `mship phase dev`'s
    # soft gate and `mship view spec`. None = default `["docs/superpowers/specs"]`
    # which matches the bundled `brainstorming` / `writing-plans` skill
    # convention. See #113.
    spec_paths: list[str] | None = None
    # When True, `mship phase dev` hard-blocks plan→dev unless a bound,
    # approved spec exists (status in approved/dispatched/implemented).
    # Default False so existing configs/tests are unaffected. See MOS-151.
    require_approved_spec: bool = False
    # Workspace-relative directory where the bundled skills write plan docs (and,
    # outside a workspace, fallback design docs). Plans live at `<docs_dir>/plans/`.
    # Does NOT affect canonical mship specs (always `specs/`) or the `spec_paths`
    # legacy spec-search default. Surfaced in `mship context` for skills.
    docs_dir: str = "docs"
    # Host-agnostic base prefix used to resolve a member's clone URL when its
    # `url` is a bare repo name or omitted (e.g. "https://github.com/atomikpanda").
    # Deliberately not named after GitHub: non-GitHub members use a full `url`.
    # See spec mship-bootstrap (MOS-180).
    default_remote: str | None = None
    relay: RelayConfig | None = None
    # Logical run-host role names available to this workspace (e.g.
    # "ios-sim-host", "android-emu-host"). Public/committed — only the role
    # *names* live here; each machine maps a role to a concrete {url, token}
    # connection in the gitignored `.mothership/run-hosts.yaml` (see
    # mship.core.run_host.RunHostStore / resolve_run_host). A repo opts into
    # one via `RepoConfig.run_host`.
    run_hosts: list[str] = []
    # Declarative reactions to task/WorkItem/PR state transitions — see spec
    # mship-lifecycle-hooks (MOS-220) and mship.core.lifecycle_hooks.
    hooks: list[HookConfig] = []
    # Fallback per-hook timeout (seconds) when a `hooks:` entry omits `timeout`.
    hooks_default_timeout: int = 30
    repos: dict[str, RepoConfig]

    @field_validator("relay", mode="before")
    @classmethod
    def parse_relay(cls, v) -> RelayConfig | None:
        if isinstance(v, dict) or v is None:
            return RelayConfig.from_mapping(v)
        return v  # already a RelayConfig instance

    @model_validator(mode="after")
    def validate_default_scope(self) -> "WorkspaceConfig":
        """default_scope may be 'all', 'none', or a list of existing repo names."""
        if isinstance(self.default_scope, str):
            if self.default_scope not in ("all", "none"):
                raise ValueError(
                    f"default_scope must be 'all', 'none', or a list of "
                    f"repo names; got {self.default_scope!r}"
                )
        elif isinstance(self.default_scope, list):
            unknown = [r for r in self.default_scope if r not in self.repos]
            if unknown:
                raise ValueError(
                    f"default_scope references unknown repos: {unknown}. "
                    f"Valid repos: {sorted(self.repos.keys())}"
                )
        return self

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

    @model_validator(mode="after")
    def validate_hooks_repo_refs(self) -> "WorkspaceConfig":
        repo_names = set(self.repos.keys())
        for hook in self.hooks:
            if hook.repo is not None and hook.repo not in repo_names:
                raise ValueError(
                    f"hooks: entry `on: {hook.on}` references unknown repo "
                    f"{hook.repo!r}. Valid repos: {sorted(repo_names)}"
                )
        return self

    @model_validator(mode="after")
    def validate_expected_branch_consistency(self) -> "WorkspaceConfig":
        groups: dict[str, list[tuple[str, str]]] = {}
        for name, repo in self.repos.items():
            if repo.git_root is None or repo.expected_branch is None:
                continue
            groups.setdefault(repo.git_root, []).append((name, repo.expected_branch))
        for root, members in groups.items():
            branches = {b for _, b in members}
            if len(branches) > 1:
                raise ValueError(
                    f"Repos sharing git_root={root!r} declare conflicting expected_branch values: "
                    + ", ".join(f"{n}={b!r}" for n, b in members)
                )
        return self


class ConfigLoader:
    """Loads and validates mothership.yaml."""

    @staticmethod
    def load(path: Path, *, require_paths: bool = True) -> WorkspaceConfig:
        with open(path) as f:
            raw = yaml.safe_load(f)

        workspace_root = path.parent

        config = WorkspaceConfig(**raw)

        # First pass: resolve paths and (when required) validate existence.
        for name, repo in config.repos.items():
            if repo.git_root is not None:
                continue
            resolved = (workspace_root / repo.path).resolve()
            repo.path = resolved
            if require_paths:
                if not resolved.is_dir():
                    raise ValueError(f"Repo '{name}' path does not exist: {resolved}")
                if not (resolved / "Taskfile.yml").exists():
                    raise ValueError(
                        f"Repo '{name}' at {resolved} has no Taskfile.yml"
                    )

        # Second pass: git_root repos validated against their parent's path.
        for name, repo in config.repos.items():
            if repo.git_root is None:
                continue
            parent = config.repos[repo.git_root]
            effective = (parent.path / repo.path).resolve()
            if require_paths:
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
        import os
        from mship.core.workspace_marker import read_marker_from_ancestor

        # 1. MSHIP_WORKSPACE env var — set-and-valid wins; set-but-invalid
        #    raises so misconfiguration fails loud instead of silently
        #    falling through to the walk-up.
        env = os.environ.get("MSHIP_WORKSPACE")
        if env:
            env_root = Path(env).resolve()
            env_yaml = env_root / "mothership.yaml"
            if env_yaml.is_file():
                return env_yaml
            raise FileNotFoundError(
                f"MSHIP_WORKSPACE={env!r} does not contain a mothership.yaml "
                f"(expected {env_yaml})"
            )

        # 2. Marker walk-up — subrepo worktrees get a `.mship-workspace`
        #    pointer from spawn. Stale markers return None silently.
        marker_root = read_marker_from_ancestor(start)
        if marker_root is not None:
            return marker_root / "mothership.yaml"

        # 3. Existing walk-up for mothership.yaml.
        current = Path(start).resolve()
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


def unique_git_roots(
    config: "WorkspaceConfig", names: list[str] | None = None
) -> list[Path]:
    """Resolved git-root path for each selected repo, de-duplicated, order-preserving.

    A repo's git root is its parent repo's path when `git_root` is set (subdir
    service), else its own path. `names=None` means all repos.
    """
    selected = names if names is not None else list(config.repos.keys())
    roots: list[Path] = []
    seen: set[Path] = set()
    for name in selected:
        repo = config.repos[name]
        root = config.repos[repo.git_root].path if repo.git_root else repo.path
        root = Path(root).resolve()
        if root not in seen:
            seen.add(root)
            roots.append(root)
    return roots

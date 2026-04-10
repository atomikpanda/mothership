# `mship init` Design Spec

## Overview

`mship init` is a guided workspace setup command that reduces the friction of adopting mothership. It supports both an interactive wizard (for humans via InquirerPy) and a flag-based mode (for agents and CI). It auto-detects repos, walks the user through configuration, optionally scaffolds starter `Taskfile.yml` files, and writes a validated `mothership.yaml`.

## Interactive Mode (Human Flow)

When run with no flags, `mship init` launches an InquirerPy-based wizard:

1. **Workspace name** — text input, defaults to current directory name
2. **Repo detection** — scans current directory, presents checkbox of candidates
3. **Manual add** — prompt to add additional repo paths not detected
4. **Repo types** — for each selected repo, ask `library` or `service` (select)
5. **Dependencies** — for each repo, multi-select from other repos for `depends_on`
6. **Taskfile scaffolding** — for repos without `Taskfile.yml`, ask whether to create a starter
7. **Env runner** — select from common options (None, dotenvx, doppler, op, custom)
8. **Write config** — validate and write `mothership.yaml`

Example session:

```
$ mship init

Welcome to Mothership! Let's set up your workspace.

? Workspace name: my-platform

Scanning for repositories...
Found 3 repos:

? Select repos to include:
  ✓ ./shared (has .git, Taskfile.yml)
  ✓ ./auth-service (has .git, package.json)
  ✓ ./api-gateway (has .git, go.mod)

? Add another repo path? (enter path or leave blank to skip):

? What type is "shared"? (library / service): library
? What type is "auth-service"? (library / service): service
? What type is "api-gateway"? (library / service): service

? What does "auth-service" depend on?
  ✓ shared

? What does "api-gateway" depend on?
  ✓ shared
  ✓ auth-service

? "auth-service" has no Taskfile.yml. Create a starter? (Y/n): y
? "api-gateway" has no Taskfile.yml. Create a starter? (Y/n): y

? Secret management (env_runner)?
  ❯ None
    dotenvx run --
    doppler run --
    op run --
    Custom...

Created: mothership.yaml
Created: auth-service/Taskfile.yml
Created: api-gateway/Taskfile.yml

Run `mship status` to verify your workspace.
```

## Non-Interactive Mode (Agent/CI Flow)

Flag-based for agents that can't use InquirerPy prompts.

### `--repo` flag

Format: `<path>:<type>[:<dep1,dep2>]`

```bash
# Single repo
mship init --name my-app --repo ./.:service

# Multi-repo with dependencies
mship init --name my-platform \
  --repo ./shared:library \
  --repo ./auth-service:service:shared \
  --repo ./api-gateway:service:shared,auth-service
```

### `--detect` flag

Auto-detect repos, include all as type `service` with no dependencies:

```bash
mship init --name my-platform --detect
```

### Other flags

- `--env-runner "dotenvx run --"` — set workspace-level env runner
- `--scaffold-taskfiles` — create starter `Taskfile.yml` in repos that don't have one (off by default in non-interactive mode)
- `--force` — overwrite existing `mothership.yaml`

### Behavior

- Non-interactive requires either `--repo` flags or `--detect` (errors with usage help otherwise)
- `--detect` and `--repo` can be combined: detected repos are merged with explicitly provided ones (explicit `--repo` entries take priority if paths overlap)
- `--name` is required in non-interactive mode
- If `mothership.yaml` already exists, errors unless `--force` is passed
- Validates the generated config through the same Pydantic validation as `ConfigLoader.load`
- TTY output: success messages via Rich. Non-TTY: JSON with the path to the created file

## Auto-Detection Logic

Scans current directory for subdirectories (one level deep) containing any of:

| Marker | What it indicates |
|--------|-------------------|
| `.git/` | Git repository |
| `Taskfile.yml` | Already has task runner |
| `package.json` | Node project |
| `go.mod` | Go project |
| `pyproject.toml` | Python project |
| `Cargo.toml` | Rust project |
| `build.gradle` | Java/Kotlin (Gradle) |
| `pom.xml` | Java (Maven) |

Rules:
- Only scans immediate subdirectories, not recursive
- Skips hidden directories and common non-repo directories (`node_modules`, `.venv`, `__pycache__`, `dist`, `build`)
- Also checks the current directory itself (for single-repo `path: .` setups)
- Returns a list of `DetectedRepo` with path and list of markers found

## Starter Taskfile Template

For repos without a `Taskfile.yml`, when the user opts in:

```yaml
version: '3'

tasks:
  test:
    desc: Run tests
    cmds:
      - echo "TODO: add test command"

  run:
    desc: Start the service
    cmds:
      - echo "TODO: add run command"

  lint:
    desc: Run linter
    cmds:
      - echo "TODO: add lint command"

  setup:
    desc: Set up development environment
    cmds:
      - echo "TODO: add setup command"
```

Stub commands that remind the user to fill them in. No language-specific guessing.

## Implementation Structure

### Core: `src/mship/core/init.py`

```python
@dataclass
class DetectedRepo:
    path: Path
    markers: list[str]  # e.g., [".git", "package.json"]

class WorkspaceInitializer:
    def detect_repos(self, workspace_path: Path) -> list[DetectedRepo]: ...
    def generate_config(
        self,
        workspace_name: str,
        repos: list[dict],  # [{name, path, type, depends_on}]
        env_runner: str | None,
    ) -> WorkspaceConfig: ...
    def write_config(self, path: Path, config: WorkspaceConfig) -> None: ...
    def write_taskfile(self, repo_path: Path) -> None: ...
```

Core logic is CLI-independent — testable without Typer or InquirerPy.

- `detect_repos` scans the filesystem and returns candidates with their markers
- `generate_config` builds a `WorkspaceConfig` Pydantic model and validates it (reuses existing validation: dependency refs, cycles, paths)
- `write_config` serializes to YAML and writes `mothership.yaml`
- `write_taskfile` writes the starter template

### CLI: `src/mship/cli/init.py`

Handles the interactive/non-interactive split:

- Interactive path: uses InquirerPy for all prompts
- Non-interactive path: parses `--repo`, `--detect`, `--name` flags
- Both paths call the same `WorkspaceInitializer` methods

Registered in `cli/__init__.py`.

### No changes to existing code

`mship init` creates files that the existing `ConfigLoader.load()` reads. The `ConfigLoader.discover()` walk-up will find the generated `mothership.yaml`. Clean separation.

## Error Handling

- `mothership.yaml` already exists → error with "use --force to overwrite" message
- No repos detected and no `--repo` flags → error with guidance
- Repo path doesn't exist → error naming the bad path
- Circular dependencies entered → caught by Pydantic validation, error with clear message
- Non-interactive missing `--name` → error with usage help

## Files

| File | Change | Purpose |
|------|--------|---------|
| `src/mship/core/init.py` | Create | WorkspaceInitializer: detection, config generation, Taskfile scaffolding |
| `src/mship/cli/init.py` | Create | CLI command: interactive wizard + flag-based mode |
| `src/mship/cli/__init__.py` | Modify | Register init module |
| `tests/core/test_init.py` | Create | WorkspaceInitializer tests |
| `tests/cli/test_init.py` | Create | CLI tests (both modes) |

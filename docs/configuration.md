# Configuration

## `mothership.yaml`

```yaml
workspace: my-platform

# Optional: wraps all task execution with a secret manager
env_runner: "dotenvx run --"

# Optional: branch naming pattern ({slug} is replaced)
branch_pattern: "feat/{slug}"

repos:
  shared:
    path: ./shared
    type: library            # "library" or "service"
    depends_on: []
    env_runner: "op run --"  # per-repo override
    tasks:
      test: unit             # override canonical task name
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
```

## Secret management (`env_runner`)

Mothership doesn't manage secrets. It delegates to your secret manager via `env_runner`:

| Tool | Config value |
|------|-------------|
| dotenvx | `dotenvx run --` |
| Doppler | `doppler run --` |
| 1Password CLI | `op run --` |
| Infisical | `infisical run --` |
| None | omit `env_runner` |

## Monorepo support (`git_root`)

For monorepos where multiple services share one git repo, use `git_root` to declare subdirectory services:

```yaml
repos:
  backend:
    path: .
    type: service
  web:
    path: web              # relative — interpreted against backend's worktree
    type: service
    git_root: backend
    depends_on: [backend]
```

Rules:
- `git_root` must reference another repo in the workspace.
- The referenced repo cannot itself have `git_root` set (no chaining).
- The subdirectory must exist and contain a `Taskfile.yml`.
- Subdirectory services still have their own `depends_on`, `tags`, `tasks`, and `start_mode`.

## Service start modes (`start_mode`)

For long-running services, set `start_mode: background`:

```yaml
repos:
  infra:
    path: ./infra
    type: service
    start_mode: background     # mship run launches and moves on
  backend:
    path: ./backend
    type: service
    start_mode: background
    depends_on: [infra]
```

With `start_mode: background`, `mship run` launches the service and continues to the next dependency tier without waiting for exit. Background services keep running until Ctrl-C propagates SIGINT through go-task to their child processes. `start_mode` only affects `mship run`. Tests and logs always run foreground.

## Healthchecks

For services that need time to become ready, declare a `healthcheck`. `mship run` waits for the healthcheck to pass before starting dependent services.

```yaml
repos:
  infra:
    path: ./infra
    type: service
    start_mode: background
    healthcheck:
      tcp: "127.0.0.1:8001"          # wait for port to accept connections
      timeout: 30s                    # optional, default 30s
      retry_interval: 500ms           # optional, default 500ms

  backend:
    path: ./backend
    type: service
    start_mode: background
    depends_on: [infra]
    healthcheck:
      http: "http://localhost:8000/health"

  web:
    path: ./web
    type: service
    start_mode: background
    depends_on: [backend]
    healthcheck:
      sleep: 3s                        # unconditional wait

  custom:
    path: ./custom
    type: service
    start_mode: background
    healthcheck:
      task: wait-for-custom            # runs `task wait-for-custom`; 0 exit = ready
```

Probe types: `tcp`, `http`, `sleep`, `task` (any one per healthcheck). Exactly one probe per healthcheck. If the probe doesn't succeed within `timeout`, the service is treated as failed and `mship run` exits non-zero. Healthchecks apply to `mship run` only.

## Task name aliasing

If your Taskfile uses different task names than mothership's defaults (`test`, `run`, `lint`, `setup`), add a `tasks:` mapping:

```yaml
repos:
  my-app:
    path: .
    type: service
    tasks:
      run: dev                 # mship run → task dev
      test: test:all           # mship test → task test:all
      lint: lint:all
      setup: infra:start
```

`mship doctor` respects the mapping when checking for standard tasks.

## Taskfile contract

Each repo needs a `Taskfile.yml` with standard task names. Mothership calls `task <name>` in each repo. Override names per repo in the `tasks` mapping. Default tasks: `test`, `run`, `lint`, `logs`, `setup`. Missing tasks are skipped gracefully.

## Workspace-level fields

Top-level keys on `mothership.yaml` (alongside `workspace`, `env_runner`, `branch_pattern`, `audit`, and `repos`, all covered above):

| Field | Meaning |
|-------|---------|
| `default_scope` | Default repo scope for `mship spawn` when `--repos` is omitted. `"all"` (default) uses every repo; `"none"` requires an explicit `--repos`; a list of repo names uses just those. (#74) |
| `spawn_confirm_threshold` | If set and a no-`--repos` spawn's effective scope exceeds N repos, require confirmation (TTY) or `--yes` (non-TTY). Unset by default. (#74) |
| `spec_paths` | Workspace-relative paths searched for specs by `mship phase dev`'s soft gate and `mship view spec`. Default: `["docs/superpowers/specs"]`. (#113) |
| `require_approved_spec` | When `true`, `mship phase dev` hard-blocks `plan → dev` unless a bound, approved spec exists. Default: `false`. (MOS-151) |
| `docs_dir` | Workspace-relative directory where the bundled skills write plan docs; plans live at `<docs_dir>/plans/`. Default: `"docs"`. Does not affect canonical specs (always `specs/`). |
| `default_remote` | Host-agnostic base URL prefix used to resolve a member's clone URL when its `url` is a bare name or omitted (member name appended). Enables `mship bootstrap` from a fresh clone. (MOS-180) |
| `relay` | Reverse-tunnel relay connection for `mship serve --relay` (see [`relay-hosting.md`](relay-hosting.md)). |
| `run_hosts` | Logical run-host role names available to the workspace (only the names are committed here). A repo opts into one via its `run_host`; each machine maps the role to a concrete `{url, token}` in the gitignored `.mothership/run-hosts.yaml`. |
| `redact` | Extra `mship export --redacted` regex patterns, unioned with the built-in set. (MOS-102) |
| `lifecycle_hooks` | Declarative reactions to task / WorkItem / PR lifecycle transitions. Named `lifecycle_hooks` (not `hooks`) to disambiguate from the git commit/push hooks. (MOS-220) |
| `lifecycle_hooks_default_timeout` | Fallback per-hook timeout in seconds when a `lifecycle_hooks:` entry omits `timeout`. Default: `30`. |

```yaml
workspace: my-platform
default_scope: none               # force explicit --repos on every spawn
spawn_confirm_threshold: 3        # confirm a no-flag spawn touching >3 repos
require_approved_spec: true       # gate plan -> dev on an approved spec
docs_dir: docs                    # plans land in docs/plans/
default_remote: https://github.com/atomikpanda   # bootstrap members from bare names

relay:
  host: relay.example.com
  ssh_port: 2222                  # optional, default 2222
  user: tunnel                    # optional; omit for the ssh default

run_hosts: [ios-sim-host, android-emu-host]   # role names; connections live in .mothership/run-hosts.yaml

redact:
  patterns:
    - "sk-[A-Za-z0-9]{20,}"                    # bare string -> a "custom" pattern
    - { name: internal-host, pattern: "corp\\.example\\.internal" }

lifecycle_hooks:
  - on: pr.merged                 # a lifecycle event (phase.entered.*, workitem.phase.*, task.finished/closed, pr.merged/closed)
    run: notify-slack             # a go-task target or shell command
    repo: backend                 # optional: run in this repo's worktree
    timeout: 60                   # optional: overrides lifecycle_hooks_default_timeout
    # required: true              # only valid on the pre-mutation events (phase.entered.* / workitem.phase.*)
```

## Per-repo fields

Additional keys on each entry under `repos:` (alongside `path`, `type`, `depends_on`, `env_runner`, `tasks`, `git_root`, `start_mode`, and `healthcheck`, covered above):

| Field | Meaning |
|-------|---------|
| `not_applicable` | Canonical task names that don't apply to this repo (e.g. `[lint]`). Skipped without warning; cannot overlap with `tasks`. (#76) |
| `tags` | Free-form tags for filtering repos via `--tag` (`mship test`/`run`/`build`). |
| `symlink_dirs` | Directories symlinked into each task worktree from the source repo (re-synced by `mship bind refresh`). |
| `bind_files` | Files (relative paths or globs, must stay inside the repo) copied into each task worktree from the source repo (re-synced by `mship bind refresh`). |
| `base_branch` | Default PR base branch for this repo (overridden by `mship finish --base` / `--base-map`; falls back to the remote default branch). |
| `expected_branch` | Branch the repo's main checkout is expected to be on; drift audit flags `unexpected_branch` otherwise. |
| `url` | Explicit clone URL for `mship bootstrap` (overrides `default_remote` + name). Non-GitHub members set a full URL here. |
| `allow_dirty` | Allow a dirty worktree without failing the drift audit. Default: `false`. |
| `allow_extra_worktrees` | Allow extra worktrees on the repo without failing the drift audit. Default: `false`. |
| `capture` | UI-capture config — a `platforms:` list `mship capture` can target (`--platform` required when more than one). |
| `run_host` | Logical run-host role this repo uses for `--remote` execution (`mship build`/`capture`). Must name an entry in the workspace `run_hosts:` list. |

```yaml
repos:
  schemas:
    path: ../schemas
    type: library
    not_applicable: [run]          # this repo has no `run` task
    tags: [generated]
    base_branch: main
    expected_branch: main
    allow_dirty: false
    allow_extra_worktrees: false
    url: https://github.com/other-org/schemas   # overrides default_remote for bootstrap
    symlink_dirs: [.github]        # symlinked into each worktree from source
    bind_files: [".env.example", "config/*.toml"]   # copied into each worktree

  ground-control:
    path: ground-control
    type: service
    run_host: android-emu-host     # `mship build/capture --remote` targets this role
    capture:
      platforms: [android, ios]    # `mship capture --platform android|ios`
```

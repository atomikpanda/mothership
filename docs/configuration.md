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
    path: web              # relative — required; interpreted against backend's worktree
    type: service
    git_root: backend      # backend is auto-ordered before web (no depends_on needed)
```

Rules:
- `git_root` must reference another repo in the workspace.
- The referenced repo cannot itself have `git_root` set (no chaining).
- A `git_root` child's `path` **must be relative** and must not contain `..`; an
  absolute path would resolve to the source checkout instead of the task worktree
  and is rejected at config load.
- A `git_root` parent is **auto-ordered before its children** — it is materialized
  first automatically, so you do NOT need to hand-add `depends_on: [parent]`.
  (Declaring `parent depends_on child` is the opposite order and is rejected as a
  dependency cycle.)
- The subdirectory must exist and contain a go-task file (`Taskfile.yml`,
  `Taskfile.yaml`, or another resolution-set spelling).
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

## Evidence storage (`evidence_storage`)

Storage mode for acceptance-criterion artifact evidence: `published`, `local`,
or `encrypted`. When unset it **inherits `spec_storage`**, mapping that field's
`committed` to `published` — a spec is committed into the workspace repo's
tree, whereas evidence is published onto an orphan branch in the repo whose PR
embeds it. Inheriting is what you want unless the cost profiles genuinely
differ — prose is bytes, screenshots are megabytes, so `spec_storage:
committed` with `evidence_storage: local` is a reasonable pairing.

Evidence may never be **more exposed** than its spec. Ordering the modes
`published` > `encrypted` > `local`, a configuration where evidence outranks
the spec is refused at config load: a plaintext screenshot beside an encrypted
spec discloses exactly what the encryption was protecting.

```yaml
evidence_storage: local   # inherits spec_storage when omitted
```

Embedding evidence in a pull-request body requires `published` — the other two
modes aren't fetchable by GitHub, so the PR names the artifact instead of
showing it. Embedding therefore means the screenshots are readable by anyone
who can read the repo the pull request targets.

## Product assumptions (`assumption_storage`, `assumption_gate`)

The product-assumptions system (#444) makes agents explicitly disposition the
assumptions a plan is built on. Two workspace-level fields govern it:

`assumption_storage` — where this workspace's L1 assumptions doc
(`docs/product_assumptions.md`) lives, mirroring `spec_storage`'s modes:
`committed` (default, plaintext + pushed), `local` (plaintext but git-ignored),
or `encrypted` (Fernet ciphertext, unreadable without `.mothership/spec-key`).
Applied transparently by `core/assumptions.py`; an invalid value fails loud at
config load.

`assumption_gate` — the L4 plan→dev gate. `off` (default) enforces nothing:
merging the feature does not change existing behaviour, so the gate rolls out
dark. `enforce` additionally requires a **fresh, fully-approved**
`PlanCheckResult` before a feature WorkItem may transition `plan → dev` — every
assumption the checker flagged as not-covered (or left un-dispositioned) must
carry an explicit `mship plan assumptions approve <axis>` sign-off. Freshness is
plan-hash bound, so editing the plan after a check re-arms the gate.

```yaml
assumption_storage: committed   # committed | local | encrypted
assumption_gate: off            # off | enforce
```

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
| `dispatch_models` | Per-mode model map for `mship dispatch` (`implementer` / `reviewer` / `standalone`). Precedence: `--model` flag > this map > built-in defaults; every built-in mode defaults to `inherit`. The sentinel means harness default behavior: the adapter omits its model selector. Explicit configured values are emitted verbatim and require a selector-capable harness adapter; an adapter without a selector rejects them rather than substituting another model. |

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
| `setup_inputs` | Manifests/lockfiles whose content decides whether a **remote** run re-runs `task setup` on the run host (glob patterns, matched inside the materialized worktree). Undeclared means setup runs on first materialization only — declaring them is what enables re-run-on-change. See [`remote-run.md`](remote-run.md). |
| `host_tools` | Optional strict selected-host mise declaration. Its manifest (and optional native lock) is validated only in the server-materialized worktree; it enables `doctor --remote` readiness inspection and explicit `bootstrap --host-tools`, never implicit provisioning. |
| `run_profiles` | Named, opt-in profile definitions for `mship run`. A profile names a configured `backend`, non-empty eligible host `roles` (each declared in workspace `run_hosts`), optional host `tags`, and JSON-compatible backend `options`. When a selected repo declares profiles, bare `mship run` uses `default_run_profile` or, in an interactive TTY, asks you to choose. |
| `run_backends` | Backend definitions: a `discover_task`, an `operations` map from operation to logical task key, and optional native `session_owner` (`android` or `flutter`). Every referenced key must exist in this repo's `tasks:` mapping; the host resolves the key rather than accepting arbitrary argv. |
| `default_run_profile` | Optional configured profile name, which must name an entry in `run_profiles`. It is the default for that repo's bare, task-bound `mship run`; without a default, a TTY chooser is used or a non-interactive run reports that `--profile` is required. |

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
    setup_inputs: [build.gradle, gradle/libs.versions.toml]   # remote runs re-run `task setup` when these change
    capture:
      platforms: [android, ios]    # `mship capture --platform android|ios`
    host_tools:
      mise:
        manifest: .mise.toml
        lock: mise.lock
      requirements:
        - {id: android-sdk, kind: sdk}
        - {id: android-adb, kind: tool}
```

When the native `mise.lock` references a dependency graph, sidecars may be
declared only as digest-pinned regular files. A lock entry may use either
`{path = "uv.lock", digest = "sha256:..."}` or the bounded directory form
`{directory = ".aube", files = {"graph.json" = "sha256:..."}}`. Every listed
file must stay beneath the materialized worktree, be non-symlinked, and match
its digest; at most 64 files per directory, 128 files total, and 1 MiB of
sidecar bytes are admitted. The directory itself is not recursively trusted.

### Profile and backend configuration

`run_profiles` is an executable, opt-in configuration surface: it selects a
reviewed backend, eligible run-host roles, and backend options. `run_backends`
maps its discovery and operation names through the repository's `tasks:` map.
The profile path requires a resolved task. A run can select more than one
repository: every selected profiled repository is discovered and preflighted
before any app launches, dependency readiness boundaries are retained, and each
launched repository receives its own recorded run ID. Repositories with no
profiles retain the ordinary `mship run` behavior.

Profiles and backends are strict configuration objects: unknown nested fields,
an unknown backend, an empty host-role list, a role outside `run_hosts`, or a
task key absent from `tasks:` rejects configuration. `session_owner` is present
only for the concrete `android` and `flutter` owners; generic backends omit it.

The portable [five-backend run-target example](https://github.com/atomikpanda/mothership/blob/main/examples/run-targets/mothership.yaml)
contains the complete schema for Android CLI, Flutter, iOS simulator, browser,
and PlatformIO profiles. Its [root Taskfile](https://github.com/atomikpanda/mothership/blob/main/examples/run-targets/Taskfile.yml)
and per-backend wrappers are executable examples, not implicit Mship setup:
[Android CLI](https://github.com/atomikpanda/mothership/tree/main/examples/run-targets/android-cli/),
[Flutter](https://github.com/atomikpanda/mothership/tree/main/examples/run-targets/flutter/),
[iOS](https://github.com/atomikpanda/mothership/tree/main/examples/run-targets/ios/),
[browser](https://github.com/atomikpanda/mothership/tree/main/examples/run-targets/browser/), and
[PlatformIO](https://github.com/atomikpanda/mothership/tree/main/examples/run-targets/platformio/).

Keep tool paths, target aliases, device identities, credentials, app templates,
and other machine-specific values in the owner-private
`$XDG_CONFIG_HOME/mothership/run-target-bindings.yaml` (or
`~/.config/mothership/run-target-bindings.yaml`), never in `mothership.yaml`.
Discovery is read-only and never installs an SDK, creates an emulator, pairs a
device, provisions a target, or falls back to an ambient target. SDKs, tools,
and the app or simulator state required by a profile must be prepared on the
selected host first. `host_tools` can diagnose or explicitly install its
declared mise tools; it does not provision platform SDK components or targets.

### Immutable declared task outputs

An exact logical task can opt into immutable generic result publication with
`task_outputs:`.  The key **must** be an existing key from that repository's
`tasks:` mapping; it is never treated as a literal Taskfile target.

```yaml
repos:
  reports:
    path: reports
    type: service
    tasks:
      publish-report: report
    task_outputs:
      publish-report:
        retention_seconds: 86400
        artifacts:
          - name: report
            relative_path: out/report.txt
            media_type: text/plain
          - name: archive
            relative_path: out/report.zip
            media_type: application/zip
```

Every artifact is an exact relative POSIX file path, not a directory, glob, or
discovery rule. Names and paths are unique; media types are concrete; nested
declaration fields are strict. The server supplies `MSHIP_OUTPUT_DIR`,
`MSHIP_OUTPUT_DECLARATION_FILE`, and `MSHIP_OUTPUT_MANIFEST`; a producer writes
only the declared files below the output directory and an exact ordered
`manifest.json` projection. It cannot choose result IDs, output locations,
retention, host provenance, or a different media type.

`required` defaults to `true`; set it to `false` only for an explicitly optional
artifact. A successful producer missing a required artifact returns
`evidence_error` with its persisted `result_id`; its immutable producing outcome
still records the successful process exit. Missing optional outputs remain
unavailable without changing that terminal status. Failed/cancelled tasks expose
only complete outputs explicitly marked `diagnostic_on_failure: true`.

`retention_seconds` expires retrieval rights while retaining result metadata.
Owner startup performs a bounded byte-retention sweep; expiry is enforced during
retrieval even before that sweep. Producer metadata currently permits only an
empty object. No arbitrary producer metadata or private environment is published.

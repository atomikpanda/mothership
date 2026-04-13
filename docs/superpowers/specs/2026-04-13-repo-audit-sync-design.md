# Repo Drift Audit & Sync (`mship audit` / `mship sync`)

**Status:** Design approved, ready for implementation plan.
**Date:** 2026-04-13

## Purpose

mship has no built-in check for repo-state drift (dirty worktrees, wrong branch, behind remote, detached HEAD, extra worktrees). `mship doctor` covers config/tool health but not git state. Without native drift detection, agents can silently operate on stale or out-of-sync repos, producing broken PRs and surprising users.

`mship audit` reports drift against a stable taxonomy of issue codes. `mship sync` performs safe fast-forward reconciliation. Both integrate with `spawn` and `finish` as opt-out gates.

## Non-Goals

- Destructive reconciliation (hard reset, auto-checkout, submodule surgery). Sync is strictly fast-forward.
- Replacing `mship doctor`. Doctor keeps config/tool checks; audit adds git-state checks. No overlap.
- Fixing drift automatically. `sync` fast-forwards only; everything else is skipped with a reason.
- Cross-repo branch coordination.

## Architecture

New top-level Typer commands `mship audit` and `mship sync`, each registered from its own module. Both sit on a pure `core/repo_state.py` layer that produces an immutable `AuditReport`. The report is the single source of truth — CLI formatting, JSON output, and the spawn/finish gate all read from it.

`sync` consumes the same report, iterates repos, and runs at most `git fetch --prune` + `git pull --ff-only` when preconditions are met. Anything else is a skip with an explicit reason.

### Files

- **Create** `src/mship/core/repo_state.py` — data classes (`Issue`, `RepoAudit`, `AuditReport`) and `audit_repos(config, shell, names=None) -> AuditReport`.
- **Create** `src/mship/core/repo_sync.py` — `sync_repos(report, config, shell) -> SyncReport`.
- **Create** `src/mship/core/audit_gate.py` — `run_audit_gate(container, repo_names, command, force) -> None` shared by spawn and finish.
- **Create** `src/mship/cli/audit.py` — `mship audit` command.
- **Create** `src/mship/cli/sync.py` — `mship sync` command.
- **Modify** `src/mship/core/config.py` — add `expected_branch`, `allow_dirty`, `allow_extra_worktrees` to `RepoConfig`; add optional `AuditPolicy` object on `WorkspaceConfig`; add validator rejecting conflicting `expected_branch` values across siblings sharing a `git_root`.
- **Modify** `src/mship/cli/worktree.py` — call `run_audit_gate` in `spawn` and `finish`; add `--force-audit` flag on both.
- **Modify** `src/mship/cli/__init__.py` — register the new sub-apps.

### Data model

```python
Severity = Literal["error", "info"]

@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    message: str

@dataclass(frozen=True)
class RepoAudit:
    name: str
    path: Path
    current_branch: str | None   # None when detached or no git
    issues: tuple[Issue, ...]

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

@dataclass(frozen=True)
class AuditReport:
    repos: tuple[RepoAudit, ...]

    @property
    def has_errors(self) -> bool: ...
    def to_json(self) -> dict: ...
```

`Issue.code` is one of the 11 stable strings from the taxonomy; free-form message never feeds logic.

## Config Additions

### Per-repo fields on `RepoConfig`

```yaml
repos:
  schemas:
    path: ../schemas
    expected_branch: marshal-refactor   # optional; enables unexpected_branch
    allow_dirty: false                  # default false
    allow_extra_worktrees: false        # default false
```

Types: `expected_branch: str | None = None`; `allow_dirty: bool = False`; `allow_extra_worktrees: bool = False`.

### Workspace-level audit policy

```yaml
audit:
  block_spawn: true      # default true
  block_finish: true     # default true
```

When the `audit:` section is absent, both defaults are `true`. Explicit `false` opts out.

### Config validation

If two `RepoConfig` entries share a `git_root` but declare different `expected_branch` values (both non-None, not equal) → reject at load with a clear message. Subdirs sharing a checkout share a branch; contradicting that is a configuration bug.

## Issue Taxonomy (11 codes)

| Code | Severity | Detection |
|------|----------|-----------|
| `path_missing` | error | Effective repo path doesn't exist. |
| `not_a_git_repo` | error | Path exists but no `.git` at the effective git root. |
| `fetch_failed` | error | `git fetch` returned non-zero. |
| `detached_head` | error | `git symbolic-ref --short HEAD` fails. |
| `unexpected_branch` | error | `expected_branch` set and current ≠ it. |
| `dirty_worktree` | error | `git status --porcelain -- <subdir>` non-empty and `allow_dirty=false`. |
| `no_upstream` | error | Current branch has no `@{u}`. |
| `behind_remote` | error | `@{u}..HEAD` empty AND `HEAD..@{u}` non-empty. |
| `diverged` | error | Both `@{u}..HEAD` and `HEAD..@{u}` non-empty. |
| `ahead_remote` | info | `@{u}..HEAD` non-empty AND `HEAD..@{u}` empty. |
| `extra_worktrees` | error | `git worktree list --porcelain` shows > 1 entry and `allow_extra_worktrees=false`. |

## Audit Algorithm

1. Resolve effective path per repo (handles `git_root` subdir pattern — same logic as `DoctorChecker`).
2. Group repos by **effective git root** — the parent repo's path for subdir entries, the repo's own path otherwise.
3. For each git root group:
   - If path missing → emit `path_missing` on every repo in the group, skip the rest.
   - If no `.git` → emit `not_a_git_repo`, skip.
   - `git fetch --prune origin` once. On failure → emit `fetch_failed` on every repo in the group, skip remaining git-wide checks (still run per-subdir `dirty_worktree`).
   - Run git-wide checks once (detached/branch/behind/ahead/diverged/no_upstream/extra_worktrees) and attach the same issues to every repo in the group.
4. Per repo (regardless of grouping), run `dirty_worktree` scoped to the repo's subdir path.

`--repos` filter narrows step 1 to named repos before grouping. Unknown names in `--repos` → exit 1 before any git operation.

## `mship audit` CLI

```
mship audit [--repos <name,name,...>] [--json]
```

Exit code: 1 if any error-severity issue anywhere in the report, else 0. Info-only issues never fail the audit.

### TTY output

```
workspace: my-workspace

schemas (feat/x):
  ✗ unexpected_branch: on 'feat/x', expected 'marshal-refactor'
  ✗ dirty_worktree: 3 uncommitted changes

cli (main):
  ✓ clean

api (cli-refactor):
  ⓘ ahead_remote: 2 commits ahead of origin/cli-refactor

2 error(s), 1 info across 3 repos
```

Clean repos emit a single `✓ clean` line.

### JSON output

```json
{
  "workspace": "my-workspace",
  "has_errors": true,
  "repos": [
    {
      "name": "schemas",
      "path": "/abs/path",
      "current_branch": "feat/x",
      "issues": [
        {"code": "unexpected_branch", "severity": "error", "message": "on 'feat/x', expected 'marshal-refactor'"},
        {"code": "dirty_worktree", "severity": "error", "message": "3 uncommitted changes"}
      ]
    }
  ]
}
```

## `mship sync` CLI

```
mship sync [--repos <name,name,...>]
```

For each repo (after `audit_repos` has populated the report):

- `path_missing` / `not_a_git_repo` / `fetch_failed` → skip with the issue message.
- `detached_head` → skip ("detached HEAD — manual action required").
- `unexpected_branch` → skip ("on X, expected Y — refusing to switch automatically").
- `dirty_worktree` → skip ("uncommitted changes — refusing to pull").
- `diverged` → skip ("local and remote diverged — manual action required").
- `no_upstream` → skip ("no upstream — set one with `git push -u`").
- `ahead_remote` only → no action, print "up to date locally (ahead of remote)".
- `behind_remote` → `git pull --ff-only`; print "fast-forwarded N commits" or skip with pull's stderr if it fails.
- No issues → no action, print "up to date".

Exit 0 if every repo either succeeded or was a benign skip (ahead-only). Exit 1 if any repo was skipped for an error condition — same exit semantics as `audit`.

### Sample output

```
schemas: skipped (unexpected_branch — refusing to switch automatically)
cli: up to date
api: fast-forwarded 3 commits
```

## Spawn / Finish Gate

Shared helper `run_audit_gate(container, repo_names, command_name, force)`:

1. Call `audit_repos(config, shell, names=repo_names)`.
2. If `report.has_errors` is false → return.
3. If `report.has_errors` is true:
   - If `force=True` → append a line to the task log (`BYPASSED AUDIT: <command> — issues: <code,code,...>`), return.
   - If `config.audit.block_<command>` is true → print the error-severity issues to stderr and exit 1.
   - If `config.audit.block_<command>` is false → print a one-line warning summary to stderr and return.

### Scoping

- `mship spawn "..." --repos schemas,cli` → gate runs on `schemas,cli` only.
- `mship spawn "..."` without `--repos` → gate runs on all repos in config.
- `mship finish` → gate runs on `task.affected_repos`.
- `mship finish --force-audit` / `mship spawn --force-audit` — bypass with a logged entry.

## Error Handling

- Any per-repo git failure surfaces as an `Issue`, not an exception — audit always completes.
- `--repos` with unknown names → exit 1 with a list of invalid names, no git operations performed.
- Config conflict (sibling subdirs with contradicting `expected_branch`) → caught at `ConfigLoader.load`, exit 1 before audit runs.

## Testing

### Unit tests (pure + subprocess against a real bare fixture)

One test per issue code, using a helper that builds an origin bare repo + a local clone in `tmp_path`:

- `path_missing`, `not_a_git_repo`, `fetch_failed` (origin URL invalid), `detached_head`, `unexpected_branch`, `dirty_worktree` (respects `allow_dirty`), `no_upstream`, `behind_remote`, `ahead_remote`, `diverged`, `extra_worktrees` (respects `allow_extra_worktrees`).

### Unit tests — grouping and config

- Monorepo: two `RepoConfig` entries with the same `git_root` → one `fetch` call (assert shell mock); both get the same branch/behind issues; `dirty_worktree` is scoped per-subdir.
- Config load rejects conflicting `expected_branch` across a `git_root` group.

### Unit tests — sync

- Clean + behind → fast-forwarded.
- Clean + ahead only → no-op ("up to date locally").
- Dirty + behind → skipped.
- Diverged → skipped.

### Integration tests

- `mship spawn` with drift in target repo and default `block_spawn` → exit 1.
- Same scenario with `--force-audit` → proceeds and task log contains the bypass line.
- `mship finish` gate uses `task.affected_repos` (drift in an unrelated repo does not block finish).
- `mship audit --json` structure round-trips (parse → serialize equals original dict shape).

## Out of Scope (post-v1)

- Parallel `git fetch` across repos.
- `--checkout` / `--force` on `sync`.
- `stale_fetch`, `untracked_files`, per-issue severity overrides in config.
- Programmatic auto-fix suggestions ("run: git pull --ff-only").

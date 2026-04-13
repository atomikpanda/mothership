# `mship switch` — Cross-Repo Context Switch

**Status:** Design approved, ready for implementation plan.
**Date:** 2026-04-13

## Purpose

A single agent working across multiple repos loses cross-repo state every time it moves between worktrees. It forgets what it changed in repo A once it's reading repo B's files, tests repo B against the wrong version of repo A, commits into the wrong branch, or works in the main checkout without noticing. mship already provides worktree isolation at spawn time; what's missing is the primitive that makes moving between worktrees within a task safe.

`mship switch <repo>` is that primitive. It's an atomic context switch: set the active repo, snapshot every dependency's current HEAD SHA, and emit a structured handoff the agent can re-inject into its context — what's new in dependencies since the last time it was here, what's the last thing it did in this repo, is the worktree clean, did tests run.

## Non-Goals

- Symbol-level change summaries (LSP / AST parsing). Commit + file summaries only.
- Shell-integration (`eval "$(mship switch … --shell)"`). v1 prints the path; caller `cd`s.
- Tagging log entries with repo metadata. Handoff uses the most recent log entry regardless of tag.
- Automatic `switch` on directory change. Explicit command only.
- Multi-agent coordination signals. v1 is single-agent orientation.

## Architecture

New top-level command `mship switch [repo]` in `src/mship/cli/switch.py`. Handoff-building logic lives in `src/mship/core/switch.py` as a pure function that takes `config + state + shell + repo_name` and returns a `Handoff` dataclass. The CLI layer formats the handoff for TTY or JSON. State is mutated once (atomic save) at the top of the switch flow; formatting never writes to state.

### Files

- **Create** `src/mship/core/switch.py` — data classes (`DepChange`, `Handoff`) and `build_handoff(...)`.
- **Create** `src/mship/cli/switch.py` — `mship switch` command.
- **Modify** `src/mship/core/state.py` — two new optional fields on `Task`.
- **Modify** `src/mship/cli/__init__.py` — register the new sub-app.
- **Modify** `src/mship/cli/status.py` — one line showing `active_repo`.
- **Modify** `src/mship/cli/view/status.py` — same line in TUI.
- **Modify** `skills/working-with-mothership/SKILL.md` — document the switch verb and session-start protocol.
- **Modify** `README.md` — add `mship switch` to CLI cheat sheet + a short prose section.

### Data additions to `Task`

```python
class Task(BaseModel):
    ...existing fields...
    active_repo: str | None = None
    last_switched_at_sha: dict[str, dict[str, str]] = {}
    # {switched_to_repo_name: {dep_repo_name: head_sha_of_dep_worktree_at_switch_time}}
```

`last_switched_at_sha[r]` is the snapshot taken the *last* time the user switched *to* repo `r`. Each inner value maps a dep-repo name → the dep worktree's HEAD SHA at that moment. This lets us compute "what changed in shared since I last switched to auth-service" as `git log <sha>..HEAD` in shared's worktree.

Legacy state files without these fields load with defaults (`None` and `{}`). No migration.

### Data model for the handoff

```python
@dataclass(frozen=True)
class DepChange:
    repo: str
    commit_count: int
    commits: tuple[str, ...]          # short log lines, "<sha> <subject>"
    files_changed: tuple[str, ...]    # paths
    additions: int
    deletions: int
    error: str | None = None          # set when worktree/SHA unavailable; other fields empty

@dataclass(frozen=True)
class Handoff:
    repo: str
    task_slug: str
    phase: str
    branch: str
    worktree_path: Path
    worktree_missing: bool
    finished_at: datetime | None
    dep_changes: tuple[DepChange, ...]
    last_log_in_repo: LogEntry | None
    drift_error_count: int
    test_status: str | None           # "pass" | "fail" | "skip" | None
```

## Command Flow

### `mship switch <repo>`

1. Load state. Exit 1 if no active task.
2. Validate `repo` is in `task.affected_repos`. Exit 1 with the list of valid names if not.
3. For every dep-repo `d` of `repo`, resolve `d`'s worktree HEAD SHA via `git rev-parse HEAD` in `d`'s worktree path. Store them under `task.last_switched_at_sha[repo]`. Missing worktrees produce no entry; the next switch will fall through to the merge-base fallback.
4. Set `task.active_repo = repo` and save state.
5. Build the handoff (see next section) and render it.

### `mship switch` (no argument)

1. Load state. Exit 1 if no active task.
2. If `task.active_repo is None` → exit 1 with `"No active repo. Run 'mship switch <repo>' first."`.
3. Otherwise, build and render the handoff for `task.active_repo`. Do NOT re-snapshot SHAs; this is a read-only render.

## Handoff Construction

Inputs: `config`, `WorkspaceState`, `ShellRunner`, `repo: str`. Output: `Handoff`.

Steps:

1. **Basics** — pull `task_slug`, `phase`, `branch` from state; resolve `worktree_path` via `task.worktrees[repo]`; set `worktree_missing = not worktree_path.exists()`; copy `task.finished_at`.

2. **Dep changes** — for each `dep` in `config.repos[repo].depends_on`:
   - Resolve `dep`'s worktree path (from `task.worktrees[dep]`).
   - If dep worktree missing on disk → emit `DepChange(repo=dep, commit_count=0, commits=(), files_changed=(), additions=0, deletions=0, error="worktree unavailable")`. Continue.
   - Determine the anchor SHA:
     - If `task.last_switched_at_sha[repo][dep]` exists → use it.
     - Else → compute `git merge-base <base_branch> <task.branch>` in the dep worktree (where `base_branch` comes from `resolve_base` with cli_base=None, falling back to the repo's configured `base_branch` or the empty string; if none, use `origin/HEAD` symbolic ref). First-switch fallback means "everything on this task branch in this dep."
   - Run `git log --format=%h %s <anchor>..HEAD` in the dep worktree. Empty → omit the dep entirely from `dep_changes`.
   - Run `git diff --stat --numstat <anchor>..HEAD` to collect files, additions, deletions.
   - Build `DepChange(...)` with the results.

3. **Last log in repo** — `log_manager.read(task.slug, last=1)`. For v1, no repo-tag filtering; return whatever the most recent entry is, or `None` if empty. (Post-v1: filter by structured repo tag once logs gain one.)

4. **Drift** — `audit_repos(config, shell, names=[repo], local_only=True)`. Count error-severity issues on this repo.

5. **Test status** — `task.test_results.get(repo)`, taking `.status` or `None`.

Any unexpected subprocess failure on a dep's `git log`/`git diff` collapses that dep into a `DepChange` with `error=<short message>`, rather than propagating the exception. The command must always produce a handoff.

## Output

### TTY

```
Switched to: auth-service (task: add-labels, phase: dev)
Branch:      feat/add-labels
Worktree:    /home/me/dev/auth-service/.worktrees/feat/add-labels

Dependencies changed since your last switch here:
  shared (2 commits):
    a3c1b2e feat: add Label type with workspace_id
    5d91ef4 refactor: re-export types from index
    files:   types.ts, index.ts  (+89 -12)

Your last log:
  "started wiring Label into middleware" (3h ago)

Drift: clean
Tests: not run yet
```

Empty section rules:
- No deps changed → single line `Dependencies: no changes since last switch`.
- `last_log_in_repo is None` → omit the `Your last log` section entirely.
- `drift_error_count > 0` → `Drift: N error(s) — run mship audit`.
- `test_status is None` → `Tests: not run yet`; else `Tests: pass` / `Tests: fail`.

Prepended warnings:
- `worktree_missing` → `⚠ worktree missing: <path> (run mship prune or mship close)` as the first line, before "Switched to:".
- `finished_at` is set → `⚠ task finished (N ago) — run mship close after merge` as the first line (or second if worktree also missing).

When invoked with no arg, the first line becomes `Currently at:` instead of `Switched to:`.

### JSON

`Handoff.to_json()` — serializes every field verbatim; `Path` becomes string, `datetime` becomes ISO 8601, `None` stays `None`. Example:

```json
{
  "repo": "auth-service",
  "task_slug": "add-labels",
  "phase": "dev",
  "branch": "feat/add-labels",
  "worktree_path": "/abs/.worktrees/feat/add-labels",
  "worktree_missing": false,
  "finished_at": null,
  "dep_changes": [
    {
      "repo": "shared",
      "commit_count": 2,
      "commits": ["a3c1b2e feat: add Label type", "5d91ef4 refactor: re-export types"],
      "files_changed": ["types.ts", "index.ts"],
      "additions": 89,
      "deletions": 12,
      "error": null
    }
  ],
  "last_log_in_repo": {
    "timestamp": "2026-04-13T10:12:00Z",
    "message": "started wiring Label into middleware"
  },
  "drift_error_count": 0,
  "test_status": null
}
```

## `mship status` Integration

`mship status` gains one line, placed below the existing `Task:` line:

```
Active repo: auth-service
```

Omitted when `task.active_repo is None`. JSON output gains `"active_repo"` field (null when unset).

`mship view status` (TUI) surfaces the same line in its `gather()` output.

## Error Handling

- No active task → exit 1, `"No active task. Run 'mship spawn' to start one."`
- `repo` not in `task.affected_repos` → exit 1, `"Unknown repo '<name>'. Valid: <list>."`
- No active repo with bare `mship switch` → exit 1, `"No active repo. Run 'mship switch <repo>' first."`
- Partial failures during handoff construction (missing dep worktree, `git log` failure) never raise — they surface as `DepChange.error` lines in the output. The command still exits 0.
- State save failure (atomic write can't land) — existing `StateManager.save` raises; CLI catches, prints, exits 1 (same pattern as every other mship command).

## Testing

### Unit tests — `tests/core/test_switch.py`

- **First switch uses merge-base fallback.** Seed a task with no `last_switched_at_sha`. Commit something in the dep's worktree. Call `build_handoff`; assert the dep change appears (all commits on the task branch).
- **Subsequent switch anchors to stored SHA.** After a first switch, commit in the dep; call `switch` again; only the new commits appear.
- **Clean deps are omitted.** Two deps, one has commits, one doesn't. Only one in `dep_changes`.
- **Missing dep worktree.** Delete the dep worktree directory; the dep appears with `error="worktree unavailable"` and zero counts.
- **Missing switched-to worktree.** Delete the repo's worktree; `worktree_missing=True`; handoff still returned.
- **Finished task.** `task.finished_at` set → `Handoff.finished_at` populated.
- **Last log absent.** Empty log for the task → `last_log_in_repo is None`.
- **Drift error count.** Dirty the repo's worktree; assert `drift_error_count >= 1`.

### CLI tests — `tests/cli/test_switch.py`

- `mship switch <valid>` records `active_repo`, saves `last_switched_at_sha`, exits 0, prints `Switched to:`.
- `mship switch` with no argument and active_repo set re-renders (no state change) with `Currently at:`.
- `mship switch` with no argument and no active_repo → exit 1.
- `mship switch bogus` → exit 1 listing valid names.
- Non-TTY → JSON with all `Handoff` fields.
- `mship status` shows `Active repo: <name>` after a switch; omits when unset.

### Integration test

Extend the `audit_workspace` fixture (or create a focused one) so `cli` depends on `shared`. Spawn a task touching both. Commit to shared's worktree. `mship switch cli` → handoff lists the shared commits. Repeat switch → no duplicated commits (anchor moved). This covers the full state → git → render loop.

## Skill & README Updates

- **Skill** (`skills/working-with-mothership/SKILL.md`): add a "Cross-repo context switches" section under "During work," documenting that the agent must call `mship switch <repo>` whenever it starts working in a different repo within the same task, and update the session-start protocol: `mship status` → `mship log` → `mship switch <current_repo>` if applicable.
- **README**: add `mship switch <repo>` / `mship switch` lines to the CLI cheat sheet; add a short "Cross-repo context switches" paragraph to the state-safety narrative explaining why the switch primitive exists.

## Out of Scope (post-v1)

- Symbol-level change summaries via LSP/AST.
- `--shell` flag emitting `cd <path>`.
- Filtering `last_log_in_repo` by structured repo tag (requires structured log refactor — roadmap item).
- Auto-running `switch` on shell `cd` hooks.
- Warning on `mship <verb>` commands that operate in the wrong worktree relative to `active_repo`.

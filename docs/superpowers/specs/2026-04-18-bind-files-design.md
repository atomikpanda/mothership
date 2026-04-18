# `bind_files` — per-worktree file copy on spawn — Design

## Context

New mship worktrees, by design, only contain tracked files from the branch checkout. Anything `.gitignored` — `.env`, `local.secrets.json`, `.vscode/settings.local.json`, a provider credentials file — is absent. Users hit this immediately: spawn a task, `cd` into the worktree, run tests, get "missing `.env`" errors, manually `cp /main/.env /worktree/.env`. The reporter in issue #39 ran that copy "twelve times."

This is specifically a pain point for the multi-repo case the README leads with. With N repos and each needing its own ignored config file, the manual copy is N operations per spawn.

Git has no native way to address this. `git worktree add` does not copy gitignored files; there is no flag, hook, or attribute for it. The philosophical reason is that git tracks content — gitignored files are outside its model. The adjacent ecosystem (direnv, secret managers) solves the problem differently and only for teams who have adopted those tools. Many real projects still ship `.env.example` (tracked) + gitignored `.env` as their config mechanism; `bind_files` removes the manual `cp` for those projects without endorsing the pattern.

## Goal

Add a per-repo config key `bind_files: list[str]` whose entries are literal relative paths or glob patterns. On `mship spawn`, each entry expands against the source repo's gitignored-leaf-file set and matched files are copied into the new worktree at the same relative path. Nested paths supported. Missing literal paths warn; globs matching zero files skip silently.

## Success criterion

A `mothership.yaml` with:

```yaml
repos:
  api:
    path: ./api
    type: service
    bind_files:
      - .env
      - .vscode/settings.local.json
      - apps/**/.env
```

Running `mship spawn "add-labels"` copies `api/.env`, `api/.vscode/settings.local.json`, and every `api/apps/*/.env` into `.worktrees/feat/add-labels/`, byte-identical to source, at the same relative paths. `mship test` in the worktree reads `.env` without a manual `cp`. Each worktree gets its own snapshot; edits in one worktree's `.env` don't affect any other worktree.

## Anti-goals

- **No symlink mode / `bind_mode`.** Copy-only v1. Extension path is open (`bind_mode: symlink` later) without breaking existing configs. Copy is correct for secrets — they're read, not mutated, and divergence per task is the desired semantic.
- **No auto-copy of all gitignored files.** Explicit opt-in list only. An auto-copy-everything-gitignored policy would try to clone `.venv/` (potentially 2GB), recursively copy `.worktrees/` (self-copy disaster), duplicate `.mothership/state.yaml` (state-in-state chaos), and pick up every tool-cache in the ecosystem. The denylist to avoid that would be longer than the allowlist `bind_files` asks for.
- **No `mship doctor` "you might want this in `bind_files`" hint.** Separate follow-up issue.
- **No refresh on `mship switch` / `sync`.** Spawn-time snapshot.
- **No cleanup on `mship close`.** Worktree teardown removes copies.
- **No workspace-level `bind_files` key.** Per-repo only. Workspace-level default can be added later without breaking the per-repo key.
- **No hardlink mode.** Not needed for secrets; copy is correct.
- **No documentation of `symlink_dirs` sharp edges** (the concurrent-install state problems discussed during brainstorming). Follow-up.

## Glob semantics

- `*` matches zero or more non-`/` characters within a single path segment.
- `?` matches exactly one non-`/` character.
- `**` matches zero or more path segments (arbitrary depth).
- Leading-dot patterns are matched literally: `.env*` matches `.env` and `.env.local`.
- Patterns that match a directory are skipped with a warning.

## How `**` stays safe despite recursive matching

The candidate set for pattern matching is NOT the full file tree of the source repo. It's the output of:

```
git ls-files --others --ignored --exclude-standard
```

Run in the source repo. This command:

- returns gitignored files at their true relative paths,
- does NOT descend into ignored directories — `.venv/`, `node_modules/`, `.worktrees/`, `.mothership/`, `.git/` are listed as directory-level entries (which `bind_files` filters out because it's files-only) but their contents are never enumerated,
- respects nested `.gitignore` files in subdirectories.

So a pattern `**/.env`:
- matches `.env` at repo root.
- matches `apps/foo/.env`, `services/billing/.env` if those are gitignored.
- does NOT match `.venv/lib/python3.14/site-packages/somepkg/.env` because `.venv/*` was never in the candidate set.

User patterns are matched against this candidate set using `pathlib.PurePosixPath.match()` with the glob semantics above.

A tracked file (not gitignored) listed in `bind_files` is a no-op: it's not in the candidate set, so no match, no copy. It's also already in the worktree because `git worktree add` put it there. Documenting this as "bind_files only acts on gitignored files" keeps the behavior predictable.

## Validation (at config load time)

Pydantic `model_validator` on `RepoConfig` runs at mship startup:

- **Absolute paths rejected:** `bind_files: [/etc/secrets]` → error naming the offending entry.
- **`..` escaping the repo rejected:** `bind_files: [../other-repo/.env]` → error.
- **Must be relative, forward-slash-delimited.**

The validation error includes the repo name and offending entry so users see which line in the YAML to fix.

## Missing-source behavior

- **Literal entry** (no `*`, `?`, or `**`) whose file doesn't exist on disk: append a warning to the spawn output, continue. Example: `bind_files: [.envv]` (typo) produces `api: bind_files source missing: .envv (will not be copied)`. Matches the existing `symlink_dirs` warning style so users recognize it.
- **Glob entry** matching zero files: silent. Globs naturally express "copy this if present," and warning on zero matches would be noisy for repos where the pattern legitimately doesn't apply.
- **Literal entry matching a directory** (shouldn't happen given the git-ignored-files enumeration filters directories, but defensive): warn and skip.

## Architecture

### Config — `src/mship/core/config.py`

New field on `RepoConfig`:

```python
class RepoConfig(BaseModel):
    ...
    symlink_dirs: list[str] = []
    bind_files: list[str] = []   # new
```

Plus a `model_validator` enforcing the path-shape rules above.

### Helpers — new in `src/mship/core/worktree.py`

Three new methods on `WorktreeManager` paralleling `_create_symlinks`:

```python
def _git_ignored_files(self, source_root: Path) -> list[PurePosixPath]:
    """Run `git ls-files --others --ignored --exclude-standard` and return leaf files."""

def _match_bind_patterns(
    self,
    patterns: list[str],
    candidates: list[PurePosixPath],
) -> list[PurePosixPath]:
    """Match user patterns against the candidate set. Dedups across patterns."""

def _copy_bind_files(
    self,
    repo_name: str,
    repo_config,
    worktree_path: Path,
) -> list[str]:
    """Top-level: resolve source root, enumerate ignored files, match patterns, copy, warn.
    Returns a list of warnings (same shape as _create_symlinks)."""
```

`shutil.copy2` is the copy primitive (preserves mtime + permissions).

`source_root` resolution reuses the existing `_create_symlinks` logic, including the `git_root` case for monorepo subdir repos:

```python
if repo_config.git_root is not None:
    parent = self._config.repos[repo_config.git_root]
    source_root = parent.path / repo_config.path
else:
    source_root = repo_config.path
```

### Spawn integration

The spawn code today runs `_create_symlinks` after worktree creation, before `task setup`. Add `_copy_bind_files` right after `_create_symlinks` in the same per-repo loop:

```
worktree create → _create_symlinks → _copy_bind_files → task setup
```

Same non-fatal warning-return style — spawn's existing warnings-surface handles display.

### Validation + matching caveat

If `pathlib.PurePosixPath.match()`'s `**` semantics surprise us on Python 3.14, fall back to a small custom matcher using `fnmatch.fnmatchcase` per segment. Covered by the tests below.

## Testing

### Unit — `tests/core/test_worktree.py` (extend existing)

**Pattern matching** (pure function, no I/O):

- Literal `.env` against `[".env", ".env.local"]` → matches just `.env`.
- Glob `.env*` against `[".env", ".env.local", "local.env"]` → matches `.env` and `.env.local`.
- `?` glob (`.env.?`) single-char match.
- `**/.env` against `[".env", "apps/foo/.env", "services/bar/.env"]` → matches all three.
- `apps/*/env` vs `apps/**/env` semantics difference (one vs multi-level).
- Multi-pattern overlap: `[".env", ".env*"]` on `[".env", ".env.local"]` → dedupes to two files.
- Empty patterns list → empty result.
- Zero matches for a pattern → that pattern contributes nothing.

**Config validation** (`tests/core/test_config.py`):

- Absolute path in `bind_files` → `ValidationError`.
- `..` segment in `bind_files` → `ValidationError`.
- Empty `bind_files` (default `[]`) → accepted.
- Valid relative path → accepted.
- Valid glob pattern → accepted.

### Integration — real git fixture

In a `tmp_path` workspace:

1. `git init -b main`, create a `.gitignore` ignoring `.env`, `.env.*`, `.venv/`, `node_modules/`, `apps/*/.env`.
2. Populate: `.env`, `.env.local`, `apps/foo/.env`, `apps/bar/.env`, `.venv/fake-file`, `node_modules/pkg/.env`.
3. Commit (tracked content only; ignored files remain on disk).
4. Add `origin`, push main (so audit passes — mship spawn's precondition).
5. Config with `bind_files: [".env", "apps/**/.env"]`.
6. Run the spawn flow end-to-end; assert that the worktree has `.env`, `apps/foo/.env`, `apps/bar/.env`, and does NOT have `.venv/fake-file` or `node_modules/pkg/.env`.
7. Byte-compare copied files to source.

### Regression — `_create_symlinks` coexistence

Config with BOTH `symlink_dirs: ["node_modules"]` and `bind_files: [".env"]`. Spawn. Assert the worktree has a `node_modules` symlink AND a copied `.env`, with neither step interfering with the other.

### Warnings surface

A `bind_files: [.envv]` (typo) on a repo that has `.env` but no `.envv` → spawn output contains the warning text `bind_files source missing: .envv`. Spawn does NOT fail.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | Copy-only v1, no symlink mode | The concrete pain is `.env` snapshot per task — copy is correct. Symlinks for files introduce the same cross-worktree shared-mutable-state problems `symlink_dirs` has for dirs (concurrent installs, lockfile divergence, dep-graph staleness). No named user needs symlink semantics for individual files. |
| 2 | Explicit opt-in list, not auto-copy of all gitignored files | Auto-copy would need a denylist for `.venv/`, `node_modules/`, `.worktrees/`, `.mothership/`, tool caches, etc., and that denylist would have to stay in sync with every ecosystem's cache conventions forever. Explicit is 3 lines of YAML; implicit is ongoing maintenance. |
| 3 | Support `**` via git's ignored-file enumeration instead of walking the filesystem | Git already has a well-defined "standard-ignored" set that excludes ignored directory contents (it doesn't descend into ignored dirs). Using that as the candidate set makes `**/.env` do the right thing without a hardcoded denylist. |
| 4 | Literal missing → warn; glob zero-match → silent | Typos in literals are a near-certain sign of user error. Zero-match globs are a legitimate "this pattern may not apply to this repo" case (e.g., `bind_files: [apps/**/.env]` in a repo that doesn't have an `apps/` tree). |
| 5 | Validation at config load, not at spawn time | Absolute paths and `..` escaping are programmer errors, not runtime conditions. Catching at load surfaces them the first time mship reads the config (via `doctor`, `status`, `init`, etc.), not only on spawn. |
| 6 | `shutil.copy2` (preserve mtime + permissions), not `shutil.copy` | `.env` files are often sourced by shell scripts (`source .env`); executable flags on scripts matter. Preserving mtime helps test caches and other tools that look at mtime. Low cost. |
| 7 | Per-repo config only; no workspace-level `bind_files` | Per-repo is the minimum that handles the reported pain. Workspace-level default is additive and can be added later if a repeat pattern emerges. |

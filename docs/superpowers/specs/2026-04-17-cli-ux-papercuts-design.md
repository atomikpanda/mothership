# CLI UX papercuts (finish stdin + hook refresh + install message) — Design

## Context

Three distinct UX papercuts surfaced during real mship usage in this session:

1. **`mship finish --body-file -` does not read stdin.** Unix convention is that `-` stands in for stdin wherever a file path is accepted. mship's finish command rejects `-` as "No such file or directory: '-'", forcing callers (scripts, agents) to write the body to a temporary file and pass the path. Hit twice in one session.

2. **`mship init --install-hooks` does not refresh stale MSHIP-managed hook bodies.** `_install_one` in `src/mship/core/hooks.py` short-circuits when it sees an existing `MSHIP-BEGIN` marker — it re-chmods the file but leaves the block body untouched. So when the hook template evolves (e.g., `_log-commit` → `_journal-commit`), users on older installs keep calling the renamed command forever. The user-visible consequence: a Typer-rendered error banner prints after every commit until the silent-exit logic ships in the binary AND the user manually removes the stale hook.

3. **`mship init --install-hooks` output lies about which hooks were installed.** The CLI prints a single line per git root: `hook installed: {root}/.git/hooks/pre-commit`. This is GitHub issue #31. The actual install touches all three hooks (`pre-commit`, `post-commit`, `post-checkout`) but the message mentions only `pre-commit`. Users can't tell whether the other two were touched at all.

All three share a subsystem (`mship init --install-hooks` and the `_install_one` primitive it sits on top of) or are adjacent enough that bundling them into one PR is tighter than three separate PRs.

## Goal

Fix all three papercuts in one coherent change. After the PR:

- `echo "body\n" | mship finish --body-file -` opens a PR with body = `body\n`.
- `mship finish --body-file -` from an interactive terminal exits 1 with `refusing to read body from an interactive TTY; pipe or redirect stdin, or use --body-file <path>`.
- `mship init --install-hooks` in a workspace with a stale `post-commit` MSHIP block rewrites it to the current template; a second run prints `up to date`; a fresh workspace gets all three hooks installed.
- `mship init --install-hooks` prints one line per hook per git root, each with its outcome (`installed` / `refreshed` / `up to date`).

## Anti-goals

- **No new flag surface.** No `--refresh-hooks`; compare-and-refresh is silent and idempotent.
- **No change to hook template bodies.** The fix is the install-time refresh logic, not the templates themselves.
- **No change to existing `--body -` behavior.** The `--body` flag already treats `-` as stdin (see `src/mship/cli/worktree.py:471` and its help text). This PR extends the same convention to `--body-file -` so the two flags are symmetric.
- **No scope creep in `mship finish` beyond the body-file path.** Every other arg and behavior is unchanged.
- **No backfill migration.** We rely on users running `mship init --install-hooks` to pick up the refresh; this is documented in the PR description and the `mship doctor` hooks check already nudges users toward it.
- **No change to MSHIP block marker syntax or format.**

## Architecture

Three self-contained changes; each touches one production file plus its tests.

### Fix 1 — `--body-file -` stdin support

**File:** `src/mship/cli/finish.py`.

Today the `finish` command in `src/mship/cli/worktree.py` (around line 467-482) resolves `custom_body` in three branches:

- `body == "-"` → reads stdin (works today, no TTY check).
- `body is not None` (any other string) → uses the string as-is.
- `body_file is not None` → `Path(body_file).read_text()` — crashes on `-` with "No such file or directory".

Fix: factor out a small helper `_read_stdin_body_or_exit(output)` that reads stdin with a TTY guard:

```python
def _read_stdin_body_or_exit(output: Output) -> str:
    import sys
    if sys.stdin.isatty():
        output.error(
            "refusing to read body from an interactive TTY; "
            "pipe or redirect stdin, or use --body-file <path>"
        )
        raise typer.Exit(code=1)
    return sys.stdin.read()
```

Wire it in both branches:

- `body == "-"` → `custom_body = _read_stdin_body_or_exit(output)` (replaces the bare `sys.stdin.read()`).
- `body_file == "-"` → new branch: `custom_body = _read_stdin_body_or_exit(output)`.
- `body_file` otherwise → existing `Path(body_file).read_text()`.

This makes `--body -` and `--body-file -` semantically identical (both read stdin with a TTY guard). The existing "empty body rejected" check runs after this branch unchanged; an empty stdin (EOF immediately) hits the same rejection as an empty `--body` or empty file.

### Fix 2 — Stale MSHIP-block refresh in `_install_one`

**File:** `src/mship/core/hooks.py`.

Today (lines 67-70):

```python
if path.exists():
    content = path.read_text()
    if HOOK_MARKER_BEGIN in content:
        _chmod_executable(path)
        return
```

This silently skips block refresh. Replace with a compare-and-replace that preserves any non-MSHIP content in the file:

1. Find `MSHIP-BEGIN` line start and `MSHIP-END` line end in the existing content.
2. Extract the existing block (from `MSHIP-BEGIN` through the newline after `MSHIP-END`).
3. Compare byte-for-byte to the block we'd emit fresh (the output of `_block(body_sh)`).
4. If identical → return `InstallOutcome.up_to_date`; no file write.
5. If different → splice the new block into the file at the same position, write, return `InstallOutcome.refreshed`.
6. Fresh install (file didn't exist, or had no MSHIP marker) → return `InstallOutcome.installed`.

**New type.** Add a small enum in `hooks.py`:

```python
class InstallOutcome(str, Enum):
    installed = "installed"
    refreshed = "refreshed"
    up_to_date = "up to date"
```

**Signature change.** `_install_one(...) -> InstallOutcome`. The public `install_hook(git_root)` wrapper (which iterates the `_HOOKS` dict and calls `_install_one` for each) changes from `-> None` to `-> dict[str, InstallOutcome]` (hook name → outcome).

### Fix 3 — Per-hook install message in `cli/init.py`

**File:** `src/mship/cli/init.py`.

Today (lines 55-56):

```python
for r in installed:
    output.success(f"hook installed: {r}/.git/hooks/pre-commit")
```

After: `installed` becomes a list of `(root, dict[str, InstallOutcome])` tuples. Render one `output.*` line per hook per root, with the outcome controlling the color level:

- `installed` → `output.success` (green)
- `refreshed` → `output.success` (green)
- `up_to_date` → `output.print` (default)

Output format:

```
pre-commit @ /abs/.git/hooks/: refreshed
post-commit @ /abs/.git/hooks/: up to date
post-checkout @ /abs/.git/hooks/: installed
```

For a multi-git-root workspace, each root gets its three lines; the `@ <path>` disambiguates. For a single-root workspace, three clean lines.

### Edge case: corrupt hook with `MSHIP-BEGIN` but no `MSHIP-END`

`_install_one` currently crashes if the end marker is missing (tries `content.index(HOOK_MARKER_END)`). After the refactor: if the end marker is absent, treat the file as "not one of ours" — log a warning via `Output.warning`, leave the file untouched, return a new outcome value `InstallOutcome.skipped_corrupt`. The enum gains that value; the CLI renders it in yellow. Tests cover this path so regressions can't sneak in.

## Testing

### Core / hooks (`tests/core/test_hooks.py`)

- Fresh install in an empty `.git/hooks/` dir — returns `{"pre-commit": installed, "post-commit": installed, "post-checkout": installed}`; all three files exist with MSHIP blocks matching the current templates.
- All three already-current blocks — returns `up_to_date` for each; mtimes unchanged (stat before/after).
- `post-commit` contains a stale body (MSHIP block with `_log-commit` rather than `_journal-commit`) — returns `refreshed` for that one, `up_to_date` for the other two; post-commit content now matches the current template; any non-MSHIP content in the file is preserved byte-for-byte.
- User-authored content before AND after the MSHIP block — refresh touches only the MSHIP block; user content stays byte-identical.
- Corrupt hook (MSHIP-BEGIN present, MSHIP-END missing) — returns `skipped_corrupt`; file untouched; warning logged.

### CLI / init (`tests/cli/test_init.py`)

- `mship init --install-hooks` on a single-root workspace — stdout contains exactly three lines matching the `<hook> @ <path>: <outcome>` format, one per hook.
- Same on a workspace with one stale hook — `refreshed` on that line, `up to date` on the others.
- Multi-git-root workspace (two git roots) — 3 × 2 = 6 lines, each with its root path disambiguating.

### CLI / finish (`tests/cli/test_finish.py`)

- `CliRunner().invoke(app, [..., "--body-file", "-"], input="body\n")` — opens PR with body `body\n`.
- `CliRunner().invoke(app, [..., "--body", "-"], input="body\n")` — same; symmetric.
- Empty stdin (`input=""`) — existing empty-body rejection fires for BOTH `--body -` and `--body-file -`.
- TTY error path — monkey-patch `sys.stdin.isatty` to return `True`; assert exit 1 and the verbatim error message. Run for both `--body -` and `--body-file -`.
- Mutex still enforced: `--body "x" --body-file -` errors with the existing mutex message.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | Compare-and-refresh MSHIP blocks (no new flag) | Idempotent; silent when a no-op; fixes stale-hook case transparently. Rejected "always rewrite" (churns mtimes) and "`--refresh-hooks` opt-in flag" (users won't know to reach for it). |
| 2 | TTY-detect + error on `--body-file -` | Block-until-EOF (standard Unix) hangs on accidental bare invocation. Erroring fast beats a mystery hang. Matches `gh` and other modern CLIs. |
| 3 | Both `--body` and `--body-file` accept `-` as stdin (symmetric) | `--body -` already reads stdin per the existing help text and `cli/worktree.py:471`. Extending the same convention to `--body-file` makes the two flags interchangeable modulo inline-vs-file semantics. Users don't have to remember which one takes `-`. |
| 4 | Per-hook per-root output lines with outcome suffixes | Users can tell whether each of the three hooks was installed fresh, refreshed from stale, or was already current. Closes issue #31. |
| 5 | Preserve user content around MSHIP blocks | Hooks are documented as user-editable outside the MSHIP block. Refresh must be surgical. |
| 6 | Corrupt hook → `skipped_corrupt` outcome, not a crash | Users with half-deleted blocks get a warning, not a traceback. No silent data loss. |
| 7 | No backfill migration | One-time user action (`mship init --install-hooks`) suffices. Doctor already nudges. |

# Pre-Commit Hook — `mship _check-commit` + `mship init` Installs

**Status:** Design approved, ready for implementation.
**Date:** 2026-04-15

## Purpose

Prevent the "committed in the wrong place while a task is active" failure mode at the git level, below any agent's ability to bypass through inattention. When a task is active, every commit must happen inside one of the task's worktrees — the hook refuses otherwise.

Earlier work shipped skill-doc warnings and `mship log` cwd hard-errors. Those help but don't catch the case where an agent runs `git commit` directly without touching mship first. Only a git hook catches that.

## Changes

### `mship _check-commit <toplevel>` (new hidden command)

A fast internal command the hook delegates to. Reads state and decides whether to allow a commit at `<toplevel>`.

- Exit 0 if: no state file exists, no active task, task has no worktrees, OR `<toplevel>` (after `Path.resolve()`) matches any `Path(task.worktrees[repo]).resolve()`.
- Exit 1 if: active task has worktrees and `<toplevel>` matches none of them. Stderr message:
  ```
  ⛔ mship: refusing commit — this is not a worktree for the active task 'add-labels'.
     Expected one of:
       /abs/path/to/.worktrees/feat/add-labels (tailrd)
       /abs/path/to/other/.worktrees/feat/add-labels (web)
     Current: /wrong/toplevel
     cd into the correct worktree, or use `git commit --no-verify` to override.
  ```
- Fail-open on any exception (corrupt state file, unreadable YAML, missing config): exit 0. A broken state file should not paralyze all commits.
- Registered with `hidden=True` so it doesn't clutter `mship --help`.

### Hook installer in `mship init`

For each unique effective git root across `config.repos`, write the MSHIP block to `<git_root>/.git/hooks/pre-commit`.

**Effective git root** = `config.repos[r].git_root`'s path when set, else `config.repos[r].path`. Dedupe so monorepos get one hook.

**Installed block:**

```sh
# MSHIP-BEGIN — managed by mship; edit outside this block is fine
if command -v mship >/dev/null 2>&1; then
    toplevel="$(git rev-parse --show-toplevel)"
    mship _check-commit "$toplevel" || exit 1
fi
# MSHIP-END
```

**Install algorithm:**

1. If file doesn't exist → create with:
   ```sh
   #!/bin/sh
   # git pre-commit hook
   <MSHIP block>
   ```
   `chmod +x`.
2. If file exists and contains `# MSHIP-BEGIN` → no-op (idempotent).
3. If file exists without the marker → append a blank line + the MSHIP block at end. Preserves any shebang/content above.
4. After install, `chmod +x` to guarantee git will invoke it.

`mship init --install-hooks` is a new flag that runs only the hook-install phase — repairs workspaces where hooks were manually removed or never installed (e.g., pre-feature workspaces).

### `mship doctor` check

For each unique effective git root, `doctor` checks `<git_root>/.git/hooks/pre-commit`:

- File doesn't exist → warn: "pre-commit hook missing at `<path>`. Run `mship init --install-hooks` to install."
- File exists but no `# MSHIP-BEGIN` marker → warn: "pre-commit hook at `<path>` exists but doesn't have the mship block. Run `mship init --install-hooks`."
- File exists with marker → pass.

Listed per git root, grouped with the repo's other checks.

## Architecture

```
src/mship/core/hooks.py      (new)
    install_hook(git_root: Path) -> InstallResult
    uninstall_hook(git_root: Path) -> None
    is_installed(git_root: Path) -> bool
    HOOK_BLOCK: str          # the MSHIP-BEGIN..MSHIP-END snippet
    HOOK_MARKER_BEGIN = "# MSHIP-BEGIN"
    HOOK_MARKER_END = "# MSHIP-END"

src/mship/cli/internal.py    (new)
    _check-commit <toplevel>   # hidden typer command

src/mship/cli/init.py         (modify)
    - On first init: install hooks across every git root.
    - New flag: --install-hooks (skip the rest of init; only install).

src/mship/core/doctor.py      (modify)
    - Per git root: check hook presence + marker.
```

## Non-Goals

- Tracking `--no-verify` bypasses. Git's baseline escape hatch; we accept the blind spot.
- `post-checkout` / `pre-push` / `pre-rebase` hooks. Pre-commit first; revisit if real pain surfaces.
- Uninstall on `close`. Hook is workspace-scoped and lifetime-persistent; removal is a deliberate user action via a separate `mship init --uninstall-hooks` flag (future work if needed).
- Per-user hook customization. The MSHIP block is a fixed template; users wanting custom behavior can edit the file outside our markers.
- Detecting symlinked worktrees beyond `Path.resolve()`. If a user sets up exotic bind-mounts, they accept the fallout.

## Testing

### Unit — `tests/core/test_hooks.py`

- `install_hook` creates file with shebang + MSHIP block when none exists; file is executable.
- `install_hook` is idempotent (second call with block present changes nothing).
- `install_hook` appends block to existing file without clobbering prior content.
- `is_installed` returns True when marker present, False when file missing or no marker.
- `uninstall_hook` removes the MSHIP block and preserves other content.
- `uninstall_hook` on a file with only the MSHIP block + shebang leaves a minimal valid hook file (or removes it — implementer's call; test whichever behavior ships).

### CLI — `tests/cli/test_check_commit.py`

- `mship _check-commit <any>` with no state file → exit 0, no output.
- With active task and matching toplevel → exit 0.
- With active task and non-matching toplevel → exit 1, stderr contains task slug and all expected worktree paths.
- With corrupt state file → exit 0.
- With no active task (state exists, `current_task is None`) → exit 0.
- `mship _check-commit` without the argument → Typer argument error (Typer handles this; just verify the command exists).

### Doctor — `tests/core/test_doctor.py`

- Workspace with hooks installed → no warning lines about hooks.
- Workspace with hook file missing → warning mentions `mship init --install-hooks`.
- Workspace with hook file present but no marker → warning.
- Monorepo (three `config.repos` sharing one `git_root`) → one warning (not three) when that single hook is missing.

### Integration — `tests/test_hook_integration.py` (new)

Uses real git repos in `tmp_path`.

- `mship init` on a fresh workspace installs hook files on every unique git root.
- After `spawn`, committing from the main checkout (wrong toplevel) fails with the expected stderr. Commit is NOT recorded.
- After `spawn`, committing from the task worktree succeeds.
- `mship init --install-hooks` on a workspace whose hook was manually removed re-installs and the subsequent wrong-place commit refuses again.
- `git commit --no-verify` from the wrong place succeeds (confirms the bypass works).

## Migration

Existing workspaces (created before this spec) won't have hooks. On next `mship init` they still don't get hooks unless it's the first init. So:

- Document `mship init --install-hooks` prominently in the README + skill.
- `mship doctor` surfaces the gap as a warning — existing users see it next time they run doctor.
- No automatic migration. Users run the command when they're ready.

## Out of Scope (post-v1)

- `mship init --uninstall-hooks` flag — trivial to add when needed.
- Post-commit bypass logging.
- Other hooks (pre-push, post-checkout).
- Windows hook compatibility testing (hooks are POSIX shell; git-for-windows ships bash that runs them, but we don't have Windows CI).

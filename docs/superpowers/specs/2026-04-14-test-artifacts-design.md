# Full Test Artifacts in Iteration Output

**Status:** Design approved, ready for implementation.
**Date:** 2026-04-14

## Purpose

Today's iteration JSON captures `stderr_tail` (last 40 lines) per repo. Agents and humans hit failures where the root cause lives in stdout, or above the tail window. The iteration machinery is already in place; we just need to capture full streams to disk and expose their paths.

## Changes

1. **Capture full stdout/stderr to disk per (iteration, repo).** After each repo's test finishes, write its full captured streams to:
   - `.mothership/test-runs/<task-slug>/<iteration>.<repo>.stdout`
   - `.mothership/test-runs/<task-slug>/<iteration>.<repo>.stderr`
2. **Iteration JSON gains `stdout_path` and `stderr_path` per repo** (absolute paths as strings). `stderr_tail` stays for quick-glance.
3. **Pruning cleans artifacts alongside iteration files.** When iteration N is dropped by `prune`, so are its `<iter>.<repo>.stdout` and `<iter>.<repo>.stderr` siblings.
4. **`mship close` removes the whole `test-runs/<task>/` directory** — already happens as part of worktree teardown. No change needed if the existing cleanup is recursive; verify and fix if not.

## Non-Goals

- Live streaming of stdout during test run. The files are written after the repo's run completes.
- Compression, rotation beyond the existing 20-iteration retention, or uploading artifacts anywhere.
- A new `mship logs --test <iteration> <repo>` reader. Agents can `cat` the file path from the JSON.

## Data Model Changes

Iteration JSON entry per repo:
```json
{
  "status": "fail",
  "duration_ms": 3500,
  "exit_code": 1,
  "stderr_tail": "...",
  "stdout_path": "/abs/.mothership/test-runs/add-labels/3.auth-service.stdout",
  "stderr_path": "/abs/.mothership/test-runs/add-labels/3.auth-service.stderr"
}
```

Both path fields are always set (even on `pass` — stdout can still be useful).

## Implementation Shape

- `src/mship/core/test_history.py`:
  - Extend `write_run` to accept `artifacts_dir: Path` (same as `_run_dir`) and per-repo `stdout`/`stderr` strings. Writes the two log files, sets `stdout_path`/`stderr_path` in the JSON.
  - Extend `prune` to also delete `<iter>.<repo>.{stdout,stderr}` files when dropping an iteration.
- `src/mship/cli/exec.py` (test command):
  - After the executor returns, for each repo result, pass `r.shell_result.stdout` and `r.shell_result.stderr` to `write_run`.

## Testing

- **`write_run` writes both log files per repo and records paths in JSON.** Integration check with a fake results dict including stdout/stderr strings.
- **`prune` deletes log files alongside iteration JSON.** Seed 25 iterations with artifacts; assert only the newest 20 remain — iteration files AND artifact files.
- **Integration: `mship test` writes stdout/stderr artifacts.** Run `mship test` via CliRunner; check `.mothership/test-runs/<slug>/1.<repo>.stdout` exists and contains the captured output.
- **JSON output includes the path fields.** Parse `mship test` non-TTY output; assert each repo entry has `stdout_path` and `stderr_path`.

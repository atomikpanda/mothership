# `mship test` surfaces `stderr_path` + `stdout_path` on failure — Design

## Context

GitHub issue #37 (from a real session): "`mship test` failure output shows `stderr_tail: '…Failed to run task…'` — truncated and unhelpful. I had to re-run `flutter test` manually to see which test failed and why."

Current state of the render path:

- The test executor captures stdout + stderr into `r.shell_result.stdout` / `r.shell_result.stderr`.
- `core.test_history.write_run` writes both streams to `.mothership/test-runs/<slug>/<iter>.<repo>.stdout` and `<iter>.<repo>.stderr` on disk, and stores `stdout_path` / `stderr_path` in each per-repo result dict.
- The iteration JSON (including the paths) is persisted and surfaced via `mship test` JSON output (non-TTY).
- **But the TTY render only shows `stderr_tail`** — the last 40 lines of captured stderr, with no mention of the paths.

Two consequences:

1. On failure, an agent or human reading `mship test`'s TTY output sees only the tail. If the tail gets dominated by task-runner framing (e.g., `Failed to run task "test": exit status 1`), the actual assertion/stack trace is invisible until someone opens the full file — and the file path is itself not printed.
2. For frameworks like `flutter test` and `go test` that write failures to stdout, not stderr, the stderr-only tail misses the failure entirely. `stdout_path` exists on disk but isn't surfaced anywhere.

The full logs are already there; the display path just doesn't point at them.

## Goal

On test-run failure, `mship test`'s TTY output prints the `stderr_path` (always) and the `stdout_path` (when non-empty) beneath each failing repo, alongside the existing tail. Users and agents see where to open for full context on the first render — no more "re-run the test manually to see the failure."

## Success criterion

Given a workspace with one test task that fails, `mship test` produces:

```
Test run #3  (1.2s)
  api: fail  (0.8s)
    stderr: .mothership/test-runs/add-labels/3.api.stderr
    stdout: .mothership/test-runs/add-labels/3.api.stdout
    last 20 lines of stderr:
      FAILED tests/foo/test_bar.py::test_thing — AssertionError
      …
      Failed to run task "test": exit status 1
```

When stdout is empty (common for pytest-only workflows), the `stdout:` line is omitted:

```
Test run #3  (1.2s)
  api: fail  (0.8s)
    stderr: .mothership/test-runs/add-labels/3.api.stderr
    last 20 lines of stderr:
      …
```

Passing repos render identically to today — no paths shown.

## Anti-goals

- **No new data plumbing.** `stderr_path` and `stdout_path` already exist in the per-repo results dict and on disk. The change is display-only.
- **No change to JSON output.** Non-TTY users already had the paths via the existing JSON. Confirming this via regression test; no format change.
- **No tail-source change.** Spec's "read tail from disk" option was considered and rejected — `r.shell_result.stderr` and the stderr file on disk are the same bytes; rereading from disk is pointless churn.
- **No interleaved stdout+stderr tail.** Was considered (option C in brainstorm) and rejected — the path is the escape valve. Showing the file path covers everything the tail might miss without the complexity of a second tail.
- **No change to `stderr_tail` computation.** Stays at last 40 lines captured from the subprocess.
- **No new flags.** Default behavior changes only.
- **No change to `.mothership/test-runs/` layout** or iteration file format.

## Architecture

Display-only change in `src/mship/cli/exec.py`'s `test` command TTY render loop (around lines 157–171 of the current source). Two tiny local helpers added in the same file:

- `_relpath(path_str) -> str` — shorten path for display by making it relative to cwd when possible; fall back to absolute.
- `_file_nonempty(path_str) -> bool` — check if a stdout file on disk is non-empty; used to suppress the `stdout:` line when stdout was empty (common case for pytest).

No new modules, no new dependencies, no changes to `test_history.py` or any executor code.

### Current render block (to be replaced)

```python
output.print(line)
if status == "fail" and info["stderr_tail"]:
    for tline in info["stderr_tail"].splitlines()[-20:]:
        output.print(f"    {tline}")
```

### New render block

```python
output.print(line)
if status == "fail":
    stderr_path = info.get("stderr_path")
    stdout_path = info.get("stdout_path")
    if stderr_path:
        output.print(f"    stderr: {_relpath(stderr_path)}")
    if stdout_path and _file_nonempty(stdout_path):
        output.print(f"    stdout: {_relpath(stdout_path)}")
    if info["stderr_tail"]:
        output.print("    last 20 lines of stderr:")
        for tline in info["stderr_tail"].splitlines()[-20:]:
            output.print(f"      {tline}")
```

Key display changes:

- `stderr:` line always shown on failure when `stderr_path` is present in the info dict.
- `stdout:` line shown on failure only when `stdout_path` is present AND the file has non-zero size. The size check is done at render time (O(1) `stat`) so we don't print a useless path for empty stdout.
- Tail gains a preamble (`last 20 lines of stderr:`) so its role as a preview — versus the full file referenced by `stderr:` — is explicit.
- Tail content indents one additional level (six spaces instead of four) to sit visually below its preamble.
- `_relpath` keeps paths short when the user is inside the workspace; falls back to absolute when they're not.

### Helpers (placed at module level in `exec.py`)

```python
def _relpath(path_str: str) -> str:
    """Shorten for display: relative to cwd if possible, else absolute."""
    from pathlib import Path
    try:
        return str(Path(path_str).relative_to(Path.cwd()))
    except ValueError:
        return path_str


def _file_nonempty(path_str: str) -> bool:
    from pathlib import Path
    try:
        return Path(path_str).stat().st_size > 0
    except OSError:
        return False
```

Local imports in the helpers match the pattern of similar small utilities elsewhere in the file.

## Data flow

**Test run with failure:**

1. Executor runs each repo's test. `r.shell_result.stdout` + `r.shell_result.stderr` captured per repo.
2. `exec.py` builds `per_repo` dict with `status`, `duration_ms`, `exit_code`, `stderr_tail`.
3. `exec.py` calls `write_run(..., streams=streams)`. `write_run` writes streams to disk and mutates `per_repo` in place, adding `stdout_path` and `stderr_path` keys per repo.
4. `exec.py` renders:
   - For each repo, prints the status line.
   - If the repo failed, prints `stderr: <path>`, then (if stdout is non-empty) `stdout: <path>`, then the tail.
5. JSON output (non-TTY): `output.json(payload)` where payload includes `per_repo` — which has `stderr_path` / `stdout_path` already. No change.

**Test run with all-pass:**

No failure lines rendered. Paths exist on disk (debugging info for later) but are not printed. Today's behavior preserved.

## Error handling

- **`info.get("stderr_path")` returns None** (write_run failed to write streams, or streams dict was empty): skip the `stderr:` line, still show the tail. Falls back to today's behavior. No user-visible error.
- **`stdout_path` points at a file that was deleted between write and render** (extreme edge case — `prune` doesn't run mid-command, so this shouldn't happen): `_file_nonempty` catches `OSError` and returns False. Line is skipped. No error.
- **`Path.cwd()` outside the workspace** (user ran `mship test` with `--task` from an unrelated directory): `_relpath` falls back to absolute path. Still usable.
- **Non-ASCII paths:** `Path` / `str(...)` handle UTF-8 natively. No special handling.
- **Windows path separators:** `Path` normalizes in `str(...)`. Display shows native separators.

## Testing

### Unit — `tests/cli/test_exec.py` (extend)

1. **Failure renders `stderr_path`.** Mock executor returns a fail result for one repo. After test-run completes, iteration file contains `stderr_path`. Invoke `mship test` via `CliRunner`. Assert the captured output contains `stderr:` and the relative path to the stderr file under `.mothership/test-runs/…`.

2. **Failure with non-empty stdout renders `stdout_path`.** Same setup, but the mock's stdout bytes are non-empty so `<iter>.<repo>.stdout` has content. Invoke `mship test`. Assert `stdout:` line is present and points at the stdout file.

3. **Failure with empty stdout does NOT render `stdout_path`.** Mock's stdout is empty; `<iter>.<repo>.stdout` file is zero bytes. Invoke `mship test`. Assert `stderr:` line is present but `stdout:` line is absent.

4. **Passing repo does NOT render paths.** Control case. Mock returns pass; no path lines appear for that repo.

5. **Mixed pass + fail.** Two repos; one passes, one fails. Assert the passing repo's output is clean and the failing repo's output includes `stderr:` line.

6. **JSON output unchanged** (regression). Non-TTY invocation (e.g., `result = runner.invoke(..., color=False)`). Parse the JSON; assert each failing repo entry has `stderr_path` and `stdout_path` keys. This test should already pass today; it guards that the display change didn't accidentally remove the JSON fields.

7. **Tail preamble rendered.** Failure case where `stderr_tail` is non-empty. Assert the captured output contains the literal string `last 20 lines of stderr:`.

### Unit — `_relpath` / `_file_nonempty` in isolation

8. **`_relpath` returns relative when cwd is parent.** Create a path `/tmp/x/y/z`, `monkeypatch.chdir(/tmp/x)`. `_relpath("/tmp/x/y/z")` → `"y/z"`.
9. **`_relpath` returns absolute when cwd is unrelated.** `chdir` to `/tmp/other`; the path `/tmp/x/y/z` doesn't relate. `_relpath(...)` returns the absolute string unchanged.
10. **`_file_nonempty` returns True for a non-empty file; False for empty; False for missing.** Three assertions.

### Regression

- Existing `tests/cli/test_exec.py` tests for `mship test` render stay green. The display change is additive; passing-repo renders are unchanged.
- Full `pytest tests/` stays green.

### No manual smoke

The test matrix above covers the render surface. A real-task smoke requires an actual failing test suite in a configured workspace — too much setup for marginal gain over the unit tests.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | Display-only change; no new data plumbing | Paths already exist in the per-repo dict via `write_run`. Adding them is pure render work. |
| 2 | Show `stderr:` line always on failure, `stdout:` only when non-empty | Stderr is the high-signal stream for test failures. Stdout is often empty for pytest-only flows; printing a useless path adds noise without information. |
| 3 | Relative path via `_relpath`, absolute fallback | Keeps display short when users are inside the workspace (common case); still usable when they're not. Matches what most tools do (pytest, cargo, go test). |
| 4 | Tail preamble `"last 20 lines of stderr:"` | Makes the tail's role as a preview explicit. Without the preamble, a reader might assume the tail is the full log. |
| 5 | Don't change `stderr_tail` computation | Reading tail from disk vs. from `shell_result.stderr` is a no-op — they're the same bytes. Rejected extra complexity. |
| 6 | Don't add a combined stdout+stderr tail | Brainstorming option C. The path-based escape valve covers the flutter/go-test case without adding a second tail source. If a future issue shows the stdout-only-failure case needs a tail preview, extend then. |
| 7 | No change to JSON output | Already correct. Regression test guards it. |
| 8 | No new flags | Default behavior change is strictly better. Nothing to opt out of. |

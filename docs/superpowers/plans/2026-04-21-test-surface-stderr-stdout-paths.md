# `mship test` Surface stderr_path + stdout_path on Failure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `mship test`'s TTY render prints `stderr: <path>` on every failing repo and `stdout: <path>` when the stdout file is non-empty, above the existing stderr tail. Closes issue #37.

**Architecture:** Display-only change in `src/mship/cli/exec.py`'s `test` command TTY render loop (around lines 157–171). Two tiny module-level helpers (`_relpath`, `_file_nonempty`) for display formatting. `stderr_path` / `stdout_path` are already populated in the per-repo results dict by `write_run`'s streams handling — no data plumbing required.

**Tech Stack:** Python 3.14, stdlib `pathlib`, pytest, `typer.testing.CliRunner`.

**Reference spec:** `docs/superpowers/specs/2026-04-21-test-surface-stderr-stdout-paths-design.md`
**Closes:** #37

---

## File structure

**Modified files:**
- `src/mship/cli/exec.py` — add `_relpath` and `_file_nonempty` module-level helpers; update the `test` command's TTY render block to emit `stderr:` and conditional `stdout:` lines on failure, add `"last 20 lines of stderr:"` preamble to the tail.
- `tests/cli/test_exec.py` — 7 new tests covering failure-render (stderr path always, stdout path only when non-empty, tail preamble, mixed pass/fail), JSON regression, and the two helpers in isolation.

**Unchanged files:**
- `src/mship/core/test_history.py` — already writes `<iter>.<repo>.stderr` / `.stdout` files and mutates the results dict with `stderr_path` / `stdout_path`. No schema change.
- `src/mship/core/executor.py` — test-run capture unchanged.
- `.mothership/test-runs/…` layout, iteration JSON format — unchanged.

**Task ordering:**
- Task 1 lands the helpers + the render changes + all new tests in one TDD pass. They're tightly coupled (same file, same render loop) and splitting them fragments the diff without clarifying review.
- Task 2 smokes end-to-end (reinstall + a failing test fixture) and ships.

---

## Task 1: Helpers + render changes + CLI tests (TDD)

**Files:**
- Modify: `src/mship/cli/exec.py`
- Modify: `tests/cli/test_exec.py`

**Context:** `CliRunner.invoke` captures stdout as a non-TTY stream, so `output.is_tty` is False and the existing `else: output.json(payload)` branch runs. To exercise the TTY render path that we're modifying, tests must force `is_tty = True` via `monkeypatch.setattr` on the `Output` instance, OR via patching `Output.is_tty` as a property. Simplest: monkeypatch the property.

`stderr_path` / `stdout_path` are added to each per-repo result dict by `test_history.write_run` AFTER the executor returns but BEFORE the render loop runs (in-place mutation of `per_repo` via `results=per_repo, streams=streams`). By the time rendering happens, both keys exist for every repo.

- [ ] **Step 1.1: Write failing tests for the helpers in isolation**

Append to `tests/cli/test_exec.py`:

```python
# --- Helpers for test-render path surfacing (issue #37) ---


def test_relpath_returns_relative_when_cwd_is_parent(tmp_path, monkeypatch):
    from mship.cli.exec import _relpath
    (tmp_path / "a" / "b").mkdir(parents=True)
    target = tmp_path / "a" / "b" / "file.txt"
    target.write_text("")
    monkeypatch.chdir(tmp_path / "a")
    assert _relpath(str(target)) == "b/file.txt"


def test_relpath_returns_absolute_when_cwd_unrelated(tmp_path, monkeypatch):
    from mship.cli.exec import _relpath
    unrelated = tmp_path / "x"
    unrelated.mkdir()
    target = tmp_path / "y" / "file.txt"
    target.parent.mkdir()
    target.write_text("")
    monkeypatch.chdir(unrelated)
    result = _relpath(str(target))
    # Path not relative to cwd → returned as-is (absolute).
    assert result == str(target)


def test_file_nonempty_true_for_non_empty_file(tmp_path):
    from mship.cli.exec import _file_nonempty
    f = tmp_path / "a.txt"
    f.write_text("some content")
    assert _file_nonempty(str(f)) is True


def test_file_nonempty_false_for_empty_file(tmp_path):
    from mship.cli.exec import _file_nonempty
    f = tmp_path / "empty.txt"
    f.write_text("")
    assert _file_nonempty(str(f)) is False


def test_file_nonempty_false_for_missing_file(tmp_path):
    from mship.cli.exec import _file_nonempty
    f = tmp_path / "nope.txt"
    # Do not create it.
    assert _file_nonempty(str(f)) is False
```

- [ ] **Step 1.2: Run the helper tests to verify they fail**

Run: `pytest tests/cli/test_exec.py::test_relpath_returns_relative_when_cwd_is_parent tests/cli/test_exec.py::test_file_nonempty_true_for_non_empty_file -v`
Expected: FAIL with `ImportError: cannot import name '_relpath' from 'mship.cli.exec'` (and the same for `_file_nonempty`).

- [ ] **Step 1.3: Implement the helpers**

Edit `src/mship/cli/exec.py`. Find the module-level imports at the top. Near them, add (at module scope — NOT inside any function):

```python
def _relpath(path_str: str) -> str:
    """Shorten for display: relative to cwd if possible, else absolute."""
    from pathlib import Path
    try:
        return str(Path(path_str).relative_to(Path.cwd()))
    except ValueError:
        return path_str


def _file_nonempty(path_str: str) -> bool:
    """True if the path exists and has non-zero size. False on OSError."""
    from pathlib import Path
    try:
        return Path(path_str).stat().st_size > 0
    except OSError:
        return False
```

Place them after any existing module-level helpers in the file; if there are none, place them immediately after the `import` block at the top of the module, before the first function or class definition.

- [ ] **Step 1.4: Run helper tests to verify they pass**

Run: `pytest tests/cli/test_exec.py -v -k "relpath or file_nonempty"`
Expected: 5 passed.

- [ ] **Step 1.5: Write failing tests for the render behavior**

Append to `tests/cli/test_exec.py`:

```python
# --- Render behavior for test failures (issue #37) ---


def _force_tty(monkeypatch):
    """Force Output.is_tty to True for the duration of a test so the TTY
    render path runs instead of the JSON fallback."""
    from mship.cli.output import Output
    monkeypatch.setattr(Output, "is_tty", property(lambda self: True))


def test_test_failure_prints_stderr_path(configured_exec_app, monkeypatch):
    """mship test failure renders `stderr: <path>` under the failing repo."""
    _force_tty(monkeypatch)
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="FAILED tests/foo.py::test_x — AssertionError"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    result = runner.invoke(app, ["test", "--all", "--task", "test-task"])
    # Look for the stderr: path line in the render.
    assert "stderr:" in result.output, result.output
    # The printed path should contain the test-runs segment.
    assert "test-runs" in result.output
    # Tail preamble should also appear.
    assert "last 20 lines of stderr:" in result.output


def test_test_failure_prints_stdout_path_when_non_empty(configured_exec_app, monkeypatch):
    """When stdout is non-empty on a failing repo, stdout: path line appears."""
    _force_tty(monkeypatch)
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="flutter stdout contents", stderr="framing"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    result = runner.invoke(app, ["test", "--all", "--task", "test-task"])
    assert "stdout:" in result.output, result.output


def test_test_failure_suppresses_stdout_path_when_empty(configured_exec_app, monkeypatch):
    """When stdout is empty on a failing repo, stdout: line is NOT emitted."""
    _force_tty(monkeypatch)
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="FAILED tests/foo.py::test_x"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    result = runner.invoke(app, ["test", "--all", "--task", "test-task"])
    assert "stderr:" in result.output
    assert "stdout:" not in result.output, result.output


def test_test_pass_does_not_print_paths(configured_exec_app, monkeypatch):
    """Passing repos render no stderr:/stdout: lines (control)."""
    _force_tty(monkeypatch)
    workspace, mock_shell = configured_exec_app
    # Default: both repos pass (mock_shell.run_task.return_value is returncode=0).
    result = runner.invoke(app, ["test", "--task", "test-task"])
    assert "stderr:" not in result.output
    assert "stdout:" not in result.output


def test_test_mixed_pass_fail_only_shows_paths_on_fail(configured_exec_app, monkeypatch):
    """Pass repo is clean; fail repo shows paths."""
    _force_tty(monkeypatch)
    workspace, mock_shell = configured_exec_app
    # First call fails, second passes.
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="FAIL"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    result = runner.invoke(app, ["test", "--all", "--task", "test-task"])
    # stderr: should appear once (for the failing repo).
    assert result.output.count("stderr:") == 1
    # At least one repo passes, one fails.
    assert "pass" in result.output and "fail" in result.output


def test_test_json_output_still_contains_paths(configured_exec_app):
    """Non-TTY JSON output must still include stderr_path / stdout_path
    keys for every repo (regression — they were added by write_run)."""
    # CliRunner default: non-TTY. Don't force TTY → goes to JSON branch.
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="err"),
        ShellResult(returncode=0, stdout="out", stderr=""),
    ]
    result = runner.invoke(app, ["test", "--all", "--task", "test-task"])
    import json as _json
    payload = _json.loads(result.output)
    repos = payload["repos"]
    for name, info in repos.items():
        assert "stderr_path" in info, f"{name} missing stderr_path"
        assert "stdout_path" in info, f"{name} missing stdout_path"
```

- [ ] **Step 1.6: Run the render tests to verify they fail**

Run: `pytest tests/cli/test_exec.py -v -k "test_failure or test_pass or test_mixed or json_output_still"`
Expected:
- `test_test_failure_prints_stderr_path` — FAIL (render doesn't print `stderr:` yet).
- `test_test_failure_prints_stdout_path_when_non_empty` — FAIL (no `stdout:` line).
- `test_test_failure_suppresses_stdout_path_when_empty` — likely PASS (no `stdout:` today).
- `test_test_pass_does_not_print_paths` — likely PASS (no paths today).
- `test_test_mixed_pass_fail_only_shows_paths_on_fail` — FAIL (no `stderr:` today).
- `test_test_json_output_still_contains_paths` — PASS (regression check — already correct).

Any surprising early-pass/fail is fine; confirm the expected-failures will get fixed by Step 1.7.

- [ ] **Step 1.7: Update the render block**

Edit `src/mship/cli/exec.py`. Find the existing render block (around line 168–171):

```python
                output.print(line)
                if status == "fail" and info["stderr_tail"]:
                    for tline in info["stderr_tail"].splitlines()[-20:]:
                        output.print(f"    {tline}")
```

Replace with:

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

Changes:
- `if status == "fail"` split from the `info["stderr_tail"]` check so path lines render even when the tail is empty.
- `stderr:` line always rendered on failure when `stderr_path` is present.
- `stdout:` line rendered only when `stdout_path` exists and the file is non-empty.
- Tail gains preamble `"last 20 lines of stderr:"`.
- Tail indent goes from 4 to 6 spaces so it reads as nested under the preamble.

- [ ] **Step 1.8: Run all new tests**

Run: `pytest tests/cli/test_exec.py -v -k "relpath or file_nonempty or test_failure or test_pass or test_mixed or json_output_still"`
Expected: all 10 new tests pass (5 helper + 5 render/regression).

- [ ] **Step 1.9: Run the full cli-exec test file**

Run: `pytest tests/cli/test_exec.py -v`
Expected: all tests pass (10 new + existing pre-existing).

If an existing test fails, it's likely because a previous `mship test` assertion was tolerant of an output format that now has extra lines. Update the assertion to keep working without losing coverage.

- [ ] **Step 1.10: Run the full suite**

Run: `pytest tests/ 2>&1 | tail -5`
Expected: all tests pass (baseline ~910, this task adds 10).

- [ ] **Step 1.11: Commit**

```bash
git add src/mship/cli/exec.py tests/cli/test_exec.py
git commit -m "feat(test): surface stderr_path and stdout_path on failure"
mship journal "mship test TTY render now prints stderr: and stdout: paths on failure; tail labeled 'last 20 lines of stderr:'" --action committed
```

---

## Task 2: Smoke + finish PR

**Files:**
- None (verification + PR only).

**Context:** Unit + integration tests cover the render surface with mocked shell failures. A real-subprocess smoke isn't strictly required (the display change is purely string assembly from data already on disk), but one quick end-to-end run confirms the story reads naturally to a human.

- [ ] **Step 2.1: Reinstall tool**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/mship-test-surfaces-stderrpath-and-stdoutpath-on-failure
uv tool install --reinstall --from . mothership
```

- [ ] **Step 2.2: Smoke in a scratch workspace with a deliberately-failing test task**

```bash
rm -rf /tmp/test-path-smoke
mkdir -p /tmp/test-path-smoke/svc
cd /tmp/test-path-smoke

cat > mothership.yaml <<'EOF'
workspace: test-path-smoke
repos:
  svc:
    path: ./svc
    type: service
    tasks: {test: fail-hard}
EOF

# svc's Taskfile has a `fail-hard` task that writes to stdout+stderr then fails.
cat > svc/Taskfile.yml <<'EOF'
version: '3'
tasks:
  fail-hard:
    cmds:
      - echo "app startup banner" # to stdout
      - echo "FAILED tests/foo.py::test_thing — AssertionError: expected 1, got 2" >&2
      - exit 1
EOF

mkdir -p .mothership
git -C svc init -q
git -C svc commit --allow-empty -m init -q

mship spawn "smoke-test-path" 2>&1 | tail -3
```

- [ ] **Step 2.3: Run `mship test` and confirm the new render**

```bash
cd /tmp/test-path-smoke
mship test --task smoke-test-path 2>&1
```

Expected: a line like:
```
  svc: fail  (0.0s)
    stderr: .mothership/test-runs/smoke-test-path/1.svc.stderr
    stdout: .mothership/test-runs/smoke-test-path/1.svc.stdout
    last 20 lines of stderr:
      FAILED tests/foo.py::test_thing — AssertionError: expected 1, got 2
      task: Failed to run task "fail-hard": exit status 1
```

Confirm the stderr file contains the failure message:

```bash
cat /tmp/test-path-smoke/.mothership/test-runs/smoke-test-path/1.svc.stderr
```

- [ ] **Step 2.4: Cleanup**

```bash
rm -rf /tmp/test-path-smoke
```

- [ ] **Step 2.5: Full pytest final check**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/mship-test-surfaces-stderrpath-and-stdoutpath-on-failure
pytest tests/ 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 2.6: Open the PR**

Write to `/tmp/test-paths-body.md`:

```markdown
## Summary

Closes #37. `mship test`'s TTY render now prints `stderr: <path>` on every failing repo and `stdout: <path>` when stdout is non-empty, above the existing stderr tail. Users and agents get a path to the full log without having to re-run the test manually.

## Before

```
Test run #3  (1.2s)
  svc: fail  (0.8s)
    Failed to run task "test": exit status 1
```

## After

```
Test run #3  (1.2s)
  svc: fail  (0.8s)
    stderr: .mothership/test-runs/<slug>/3.svc.stderr
    stdout: .mothership/test-runs/<slug>/3.svc.stdout
    last 20 lines of stderr:
      FAILED tests/foo.py::test_thing — AssertionError: expected 1, got 2
      Failed to run task "test": exit status 1
```

The `stdout:` line is suppressed when the stdout file is empty (common for pytest-only flows). Passing repos render identically to before.

## Scope

- Display-only change. No data plumbing: `stderr_path` and `stdout_path` were already populated in the per-repo results dict by `test_history.write_run`.
- JSON output (non-TTY) unchanged — the paths were already in the payload.
- No new flags, no schema changes, no executor changes.

## Changes

- `src/mship/cli/exec.py`:
  - Two new module-level helpers: `_relpath` (cwd-relative display) and `_file_nonempty` (suppress empty stdout lines).
  - TTY render block updated: prints `stderr:` always on failure, `stdout:` when file is non-empty, and labels the tail `"last 20 lines of stderr:"`.

## Test plan

- [x] `tests/cli/test_exec.py`: 10 new tests (5 helper unit tests, 4 render-behavior CLI tests, 1 JSON regression).
- [x] Full suite: all pass.
- [x] Manual smoke: scratch workspace with a deliberately-failing test task confirms the path lines render and the files on disk contain the full output.
```

Then:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/mship-test-surfaces-stderrpath-and-stdoutpath-on-failure
mship finish --body-file /tmp/test-paths-body.md
```

Expected: PR URL returned.

---

## Done when

- [x] `_relpath` and `_file_nonempty` helpers exist at module level in `exec.py`.
- [x] TTY render on failure prints `stderr: <path>` always, `stdout: <path>` when non-empty.
- [x] Tail labeled `"last 20 lines of stderr:"`, indented 6 spaces.
- [x] Passing repos render identically to before.
- [x] JSON output includes `stderr_path` and `stdout_path` per repo (regression-tested).
- [x] 10 new tests pass; full pytest green.
- [x] Manual smoke confirms the render reads naturally and the files on disk contain the full output.

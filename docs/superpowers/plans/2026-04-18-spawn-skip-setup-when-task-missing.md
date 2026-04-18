# Spawn Skip Setup When `task` Missing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `mship spawn` silently skips the per-repo setup task when the `task` binary is not on PATH; `mship doctor` surfaces the missing binary as a `warn`-status check so the signal lives in one predictable place.

**Architecture:** Guard the setup `run_task` call in `WorktreeManager.spawn` behind `shutil.which("task") is not None` (two call-sites: git_root branch and normal-repo branch). Add a new `go-task` check to `Doctor.diagnose` that mirrors the existing `gh` pattern (`pass` when found, `warn` when missing). No new config keys, no caching.

**Tech Stack:** Python 3.14, `shutil.which`, existing Pydantic-based config, pytest + `monkeypatch`.

**Reference spec:** `docs/superpowers/specs/2026-04-18-spawn-skip-setup-when-task-missing-design.md`
**Closes:** #51

---

## File structure

**Modified files:**
- `src/mship/core/worktree.py` — add `import shutil` if missing; guard the two setup `run_task` calls with `shutil.which("task") is not None`.
- `src/mship/core/doctor.py` — add `import shutil`; add a new `go-task` check near the existing `gh` block.
- `tests/core/test_worktree.py` — add a new skip-behavior test; update 2 existing tests to monkeypatch `shutil.which` so they still exercise the setup path on machines without `task` installed.
- `tests/core/test_doctor.py` — add 2 new tests for `go-task` pass/warn.

**Unchanged files:**
- `src/mship/util/shell.py` — `run_task` stays identical. The guard lives at the caller.
- Other `run_task` callers (executor, healthcheck task probes) — out of scope per spec decision 1.

**Task ordering rationale:** Task 1 is the spawn guard — self-contained, touches one production file and one test file. Task 2 is the doctor check — similar shape, different file. Task 3 verifies end-to-end and opens the PR.

---

## Task 1: Guard `WorktreeManager.spawn` setup calls on `shutil.which("task")`

**Files:**
- Modify: `src/mship/core/worktree.py`
- Modify: `tests/core/test_worktree.py`

**Context:** The setup block exists twice in `WorktreeManager.spawn` — once inside the `git_root` sub-branch (around line 222) and once inside the normal-repo branch (around line 257). Both wrap an identical `if not skip_setup:` guard. Extend each guard with `and shutil.which("task") is not None`. When the binary isn't on PATH, the setup call is skipped silently.

- [ ] **Step 1.1: Write failing test for the skip behavior**

Append to `tests/core/test_worktree.py` (bottom of file):

```python
def test_spawn_skips_setup_when_task_binary_missing(worktree_deps, monkeypatch):
    """When `task` binary isn't on PATH, spawn skips the setup run_task
    call silently — no warning appended, no mock invocation.
    """
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    monkeypatch.setattr(
        "mship.core.worktree.shutil.which",
        lambda name: None if name == "task" else "/usr/bin/" + name,
    )
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    result = mgr.spawn("task-missing-smoke", repos=["shared"])

    # No setup warning about missing task binary
    assert not any("setup failed" in w for w in result.setup_warnings)
    # run_task was NOT called for setup (the guard short-circuits before the call)
    assert not any(
        call.kwargs.get("task_name") == "setup"
        for call in shell.run_task.call_args_list
    )
```

- [ ] **Step 1.2: Update existing `test_spawn_runs_setup_task` to monkeypatch `shutil.which`**

Existing test at `tests/core/test_worktree.py:111`:

```python
def test_spawn_runs_setup_task(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("with setup", repos=["shared"])
    shell.run_task.assert_called()
```

Replace with:

```python
def test_spawn_runs_setup_task(worktree_deps, monkeypatch):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    monkeypatch.setattr(
        "mship.core.worktree.shutil.which",
        lambda name: "/usr/local/bin/task" if name == "task" else None,
    )
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("with setup", repos=["shared"])
    shell.run_task.assert_called()
```

- [ ] **Step 1.3: Update existing `test_spawn_collects_setup_warnings_on_failure` to monkeypatch `shutil.which`**

Existing test at `tests/core/test_worktree.py:217`:

```python
def test_spawn_collects_setup_warnings_on_failure(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    # Make setup return non-zero
    shell.run_task.return_value = ShellResult(
        returncode=1, stdout="", stderr="setup task not found"
    )
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    result = mgr.spawn("warning test", repos=["shared"])
    assert len(result.setup_warnings) == 1
    assert "shared" in result.setup_warnings[0]
    assert "setup" in result.setup_warnings[0].lower()
```

Replace with:

```python
def test_spawn_collects_setup_warnings_on_failure(worktree_deps, monkeypatch):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    monkeypatch.setattr(
        "mship.core.worktree.shutil.which",
        lambda name: "/usr/local/bin/task" if name == "task" else None,
    )
    # Make setup return non-zero
    shell.run_task.return_value = ShellResult(
        returncode=1, stdout="", stderr="setup task not found"
    )
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    result = mgr.spawn("warning test", repos=["shared"])
    assert len(result.setup_warnings) == 1
    assert "shared" in result.setup_warnings[0]
    assert "setup" in result.setup_warnings[0].lower()
```

- [ ] **Step 1.4: Run the three tests to verify expected state**

Run: `pytest tests/core/test_worktree.py::test_spawn_skips_setup_when_task_binary_missing tests/core/test_worktree.py::test_spawn_runs_setup_task tests/core/test_worktree.py::test_spawn_collects_setup_warnings_on_failure -v`

Expected:
- `test_spawn_skips_setup_when_task_binary_missing` — **FAIL** (production guard doesn't exist yet; mock returns None for "task" but the spawn still calls run_task).
- `test_spawn_runs_setup_task` — **PASS** (production doesn't check shutil.which, so run_task is still called; the added monkeypatch is a no-op for now).
- `test_spawn_collects_setup_warnings_on_failure` — **PASS** (same reason).

- [ ] **Step 1.5: Add the production guard in `worktree.py`**

Edit `src/mship/core/worktree.py`.

Add `import shutil` at the top of the module if it's not already there. Existing imports:

```python
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
```

(Verify — `shutil` is already used for `shutil.copy2` in the `_copy_bind_files` method, so the import is almost certainly present. Add only if the file doesn't have it.)

Find the first setup block (inside the `git_root` branch — the one after the symlink + bind_file extends, ending with the `continue`):

```python
                if not skip_setup:
                    actual_setup = repo_config.tasks.get("setup", "setup")
                    setup_result = self._shell.run_task(
                        task_name="setup",
                        actual_task_name=actual_setup,
                        cwd=effective,
                        env_runner=repo_config.env_runner or self._config.env_runner,
                    )
                    if setup_result.returncode != 0:
                        setup_warnings.append(
                            f"{repo_name}: setup failed (task '{actual_setup}') — "
                            f"{setup_result.stderr.strip()[:200]}"
                        )
```

Replace with:

```python
                if not skip_setup and shutil.which("task") is not None:
                    actual_setup = repo_config.tasks.get("setup", "setup")
                    setup_result = self._shell.run_task(
                        task_name="setup",
                        actual_task_name=actual_setup,
                        cwd=effective,
                        env_runner=repo_config.env_runner or self._config.env_runner,
                    )
                    if setup_result.returncode != 0:
                        setup_warnings.append(
                            f"{repo_name}: setup failed (task '{actual_setup}') — "
                            f"{setup_result.stderr.strip()[:200]}"
                        )
```

Find the second setup block (inside the normal-repo branch, ending just before `base_branch = workspace_default_branch_from_config(...)`):

```python
            if not skip_setup:
                actual_setup = repo_config.tasks.get("setup", "setup")
                setup_result = self._shell.run_task(
                    task_name="setup",
                    actual_task_name=actual_setup,
                    cwd=wt_path,
                    env_runner=repo_config.env_runner or self._config.env_runner,
                )
                if setup_result.returncode != 0:
                    setup_warnings.append(
                        f"{repo_name}: setup failed (task '{actual_setup}') — "
                        f"{setup_result.stderr.strip()[:200]}"
                    )
```

Replace with:

```python
            if not skip_setup and shutil.which("task") is not None:
                actual_setup = repo_config.tasks.get("setup", "setup")
                setup_result = self._shell.run_task(
                    task_name="setup",
                    actual_task_name=actual_setup,
                    cwd=wt_path,
                    env_runner=repo_config.env_runner or self._config.env_runner,
                )
                if setup_result.returncode != 0:
                    setup_warnings.append(
                        f"{repo_name}: setup failed (task '{actual_setup}') — "
                        f"{setup_result.stderr.strip()[:200]}"
                    )
```

- [ ] **Step 1.6: Re-run the three tests to verify they all pass**

Run: `pytest tests/core/test_worktree.py::test_spawn_skips_setup_when_task_binary_missing tests/core/test_worktree.py::test_spawn_runs_setup_task tests/core/test_worktree.py::test_spawn_collects_setup_warnings_on_failure -v`

Expected: 3 passed.

- [ ] **Step 1.7: Run the full worktree test file**

Run: `pytest tests/core/test_worktree.py -v`

Expected: all tests pass. If a different test relies on `shell.run_task` being called for setup and fails due to the new guard (because the test environment lacks `task`), update it by adding a `monkeypatch.setattr("mship.core.worktree.shutil.which", lambda name: "/usr/local/bin/task" if name == "task" else None)` block at the top.

- [ ] **Step 1.8: Commit**

```bash
git add src/mship/core/worktree.py tests/core/test_worktree.py
git commit -m "feat(worktree): skip setup silently when task binary missing"
mship journal "spawn now gates setup on shutil.which('task'); existing setup tests updated with monkeypatch" --action committed
```

---

## Task 2: `mship doctor` surfaces `go-task` as a check

**Files:**
- Modify: `src/mship/core/doctor.py`
- Modify: `tests/core/test_doctor.py`

**Context:** Doctor today reports on `gh`, `env_runner`, and other tools. Add a `go-task` row that always fires: `pass` when `shutil.which("task")` returns a path, `warn` when it returns None. Message for the warn case points at https://taskfile.dev and explains the consequence ("mship will skip per-repo setup on spawn").

- [ ] **Step 2.1: Write failing tests**

Append to `tests/core/test_doctor.py` (bottom of the file):

```python
def test_doctor_go_task_pass_when_binary_present(workspace: Path, monkeypatch):
    monkeypatch.setattr(
        "mship.core.doctor.shutil.which",
        lambda name: "/usr/local/bin/task" if name == "task" else None,
    )
    config = ConfigLoader.load(workspace / "mothership.yaml")
    from mship.core.doctor import DoctorChecker
    shell = MagicMock(spec=ShellRunner)
    shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    report = DoctorChecker(config, shell).diagnose()
    go_task_checks = [c for c in report.checks if c.name == "go-task"]
    assert len(go_task_checks) == 1
    assert go_task_checks[0].status == "pass"
    assert "go-task found" in go_task_checks[0].message


def test_doctor_go_task_warn_when_binary_missing(workspace: Path, monkeypatch):
    monkeypatch.setattr(
        "mship.core.doctor.shutil.which",
        lambda name: None,
    )
    config = ConfigLoader.load(workspace / "mothership.yaml")
    from mship.core.doctor import DoctorChecker
    shell = MagicMock(spec=ShellRunner)
    shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    report = DoctorChecker(config, shell).diagnose()
    go_task_checks = [c for c in report.checks if c.name == "go-task"]
    assert len(go_task_checks) == 1
    assert go_task_checks[0].status == "warn"
    assert "not installed" in go_task_checks[0].message
    assert "https://taskfile.dev" in go_task_checks[0].message
    assert "skip per-repo setup on spawn" in go_task_checks[0].message
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `pytest tests/core/test_doctor.py::test_doctor_go_task_pass_when_binary_present tests/core/test_doctor.py::test_doctor_go_task_warn_when_binary_missing -v`

Expected: both FAIL with `assert len(go_task_checks) == 1` (no such check yet) — `len` will be 0.

- [ ] **Step 2.3: Add the `go-task` check to `Doctor.diagnose`**

Edit `src/mship/core/doctor.py`.

Add `import shutil` at the top of the module. The existing imports are:

```python
import os
from dataclasses import dataclass, field
from pathlib import Path
```

Add `import shutil` immediately after `import os`:

```python
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
```

Find the `gh` check in `diagnose()` (around line 220):

```python
        # gh CLI
        gh_result = self._shell.run("gh auth status", cwd=Path("."))
        if gh_result.returncode == 0:
            report.checks.append(CheckResult(name="gh", status="pass", message="authenticated"))
        elif gh_result.returncode == 127:
            report.checks.append(CheckResult(name="gh", status="warn", message="gh CLI not installed (optional — needed for mship finish)"))
        else:
            report.checks.append(CheckResult(name="gh", status="warn", message="gh CLI not authenticated (run gh auth login)"))
```

Immediately after that block (before the `# Dev-mode trap:` comment), insert:

```python
        # go-task binary — signals whether spawn will run per-repo setup tasks
        if shutil.which("task") is not None:
            report.checks.append(CheckResult(
                name="go-task",
                status="pass",
                message="go-task found",
            ))
        else:
            report.checks.append(CheckResult(
                name="go-task",
                status="warn",
                message=(
                    "go-task not installed (https://taskfile.dev); "
                    "mship will skip per-repo setup on spawn"
                ),
            ))
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `pytest tests/core/test_doctor.py -v`

Expected: all tests pass (the 2 new + existing).

- [ ] **Step 2.5: Commit**

```bash
git add src/mship/core/doctor.py tests/core/test_doctor.py
git commit -m "feat(doctor): add go-task binary check (pass/warn)"
mship journal "mship doctor now reports go-task binary presence; signals missing binary via warn row" --action committed
```

---

## Task 3: Manual smoke + finish PR

**Files:**
- None (verification only).

**Context:** Exercise the change end-to-end in a scratch workspace, with and without `task` on PATH.

- [ ] **Step 3.1: Reinstall tool**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/spawn-skips-setup-when-task-binary-missing-and-doctor-surfaces-it
uv tool install --reinstall --from . mothership
```

- [ ] **Step 3.2: Build scratch workspace**

```bash
rm -rf /tmp/task-missing-smoke
mkdir -p /tmp/task-missing-smoke
cd /tmp/task-missing-smoke

cat > mothership.yaml <<'EOF'
workspace: task-missing-smoke
repos:
  svc:
    path: ./svc
    type: service
EOF

mkdir -p svc .mothership
git -C svc init -q
git -C svc commit --allow-empty -m "init" -q
git -C svc remote add origin /tmp/task-missing-smoke/fake-remote 2>/dev/null || true
```

- [ ] **Step 3.3: Smoke — spawn (this machine doesn't have `task` installed, so this exercises the no-task path)**

First confirm task isn't on PATH:

```bash
which task; echo "task which-exit: $?"
```

Expected: empty output, `which-exit: 1` (not found). If task IS installed on this machine, skip Steps 3.3–3.4 and rely on the unit-test coverage; Step 3.5 below is the task-present smoke.

With task confirmed missing:

```bash
cd /tmp/task-missing-smoke
mship spawn "smoke-no-task" 2>&1 | tail -20
```

Expected: the `setup_warnings` array in the JSON output does NOT contain `"task: not found"` or `"setup failed"`. Before the fix, it did.

Cleanup the spawned task so the next smoke runs cleanly:

```bash
mship close --yes --abandon --task smoke-no-task 2>&1 | tail -3
```

- [ ] **Step 3.4: Smoke — doctor reports the missing binary**

```bash
mship doctor 2>&1 | grep -i go-task
```

Expected: one line matching `warn   go-task   go-task not installed (https://taskfile.dev); mship will skip per-repo setup on spawn`.

- [ ] **Step 3.5: Smoke — doctor with `task` on PATH (only run if task IS installed locally)**

```bash
which task && mship doctor 2>&1 | grep -i go-task
```

If `task` is installed: expect `pass   go-task   go-task found`.
If not installed: the pass case is covered by the unit test in Task 2; note the skip and proceed.

- [ ] **Step 3.6: Cleanup**

```bash
rm -rf /tmp/task-missing-smoke
```

- [ ] **Step 3.7: Full pytest final check**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/spawn-skips-setup-when-task-binary-missing-and-doctor-surfaces-it
pytest tests/ 2>&1 | tail -3
```

Expected: 870+ passed.

- [ ] **Step 3.8: Open PR**

Write this body to `/tmp/task-missing-body.md`:

```markdown
## Summary

Closes #51. `mship spawn` no longer prints `"mothership: setup failed (task 'setup') — /bin/sh: 1: task: not found"` on every invocation when go-task isn't installed. Instead:

- **`mship spawn`** silently skips the setup call when `shutil.which("task")` returns None.
- **`mship doctor`** surfaces the missing binary as a `warn`-status check (`go-task not installed (https://taskfile.dev); mship will skip per-repo setup on spawn`).

Previously, a session with three spawns produced three identical warnings. Now the signal lives in one predictable place (doctor) and appears exactly as many times as the user runs `mship doctor`.

## Scope

- Only the spawn-setup `run_task` invocation is gated. Other `run_task` callers (`mship run`, `mship test`, healthcheck task probes) still surface "task: not found" if the user tries to use those commands — those are one-time actionable errors, not repeated noise.
- No new config key, no caching, no TTL.
- Doctor's new check always fires (pass/warn), matching the existing `gh` pattern.

## Changes

- `src/mship/core/worktree.py` — `if not skip_setup:` guard extended with `and shutil.which("task") is not None` at the two setup call-sites (git_root branch and normal-repo branch).
- `src/mship/core/doctor.py` — new `go-task` check added near the existing `gh` block.

## Test plan

- [x] `tests/core/test_worktree.py`: 1 new test (`test_spawn_skips_setup_when_task_binary_missing`). 2 existing tests (`test_spawn_runs_setup_task`, `test_spawn_collects_setup_warnings_on_failure`) updated to monkeypatch `shutil.which` so they still exercise the setup path on machines without go-task.
- [x] `tests/core/test_doctor.py`: 2 new tests for the `go-task` pass and warn cases.
- [x] Full suite: 870+ passed.
- [x] Manual smoke: spawn in a go-task-less environment produces no setup warning; doctor surfaces the missing binary.
```

Then:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/spawn-skips-setup-when-task-binary-missing-and-doctor-surfaces-it
mship finish --body-file /tmp/task-missing-body.md
```

Expected: PR URL returned.

---

## Done when

- [x] `WorktreeManager.spawn` skips setup silently when `shutil.which("task")` is None (both branches).
- [x] `Doctor.diagnose` emits a `go-task` `CheckResult` — `pass` when binary found, `warn` with messaging otherwise.
- [x] No other `run_task` callers changed.
- [x] All existing `test_worktree.py` and `test_doctor.py` tests pass. 3 tests updated (one new, two monkeypatched); 2 tests added for doctor.
- [x] Full pytest green (870+).
- [x] Manual smoke: spawn clean without task on PATH; doctor shows the warn row.

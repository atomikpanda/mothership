# Healthcheck Fast-Fail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a background service's `run` Popen exits non-zero during healthcheck wait, detect the crash within one retry interval and surface the real exit code in the user-visible error, instead of waiting the full healthcheck timeout.

**Architecture:** Add an optional `proc` keyword argument to `HealthcheckRunner.wait()`. Inside the existing retry loop, poll `proc.poll()` before each probe; if the process has exited non-zero, return `HealthcheckResult(ready=False, message="background process exited with code N ...")` immediately. Exit-0 is ignored so `docker run -d`-style handoffs keep working. The executor builds a `repo_to_proc` map during tier execution and passes the matching Popen into `wait()`.

**Tech Stack:** Python 3.14, stdlib `subprocess.Popen.poll()`, pytest.

**Reference spec:** `docs/superpowers/specs/2026-04-18-healthcheck-fast-fail-design.md`

---

## File structure

**Modified files:**
- `src/mship/core/healthcheck.py` — `HealthcheckRunner.wait()` gains an optional `proc` kwarg; the retry loop polls it on each iteration.
- `src/mship/core/executor.py` — `execute()` builds a `repo_to_proc` dict during the tier spawn step and threads the Popen into `self._healthcheck.wait(...)`.
- `tests/core/test_healthcheck.py` — new unit tests for the `proc` parameter.
- `tests/core/test_executor.py` — new integration test that crashes a real subprocess.

**Unchanged files:**
- `src/mship/cli/exec.py` — CLI's failure handler already kills background process groups and exits 1; no change needed.
- `src/mship/util/shell.py` — `run_streaming` is the Popen source; no change to its signature or behavior.

**Task ordering rationale:** Task 1 is self-contained (healthcheck helper + its unit tests). Task 2 wires it through the executor without end-to-end coverage. Task 3 adds the integration test that catches regressions in the wiring. Task 4 verifies and opens the PR.

---

## Task 1: Add `proc` parameter to `HealthcheckRunner.wait()`

**Files:**
- Modify: `src/mship/core/healthcheck.py`
- Modify: `tests/core/test_healthcheck.py`

**Context:** `HealthcheckRunner.wait()` currently loops over tcp/http/task probes until ready or the deadline. We add an optional `proc` kwarg. Before each probe, if `proc` is provided, call `proc.poll()`. A non-zero return means the background subprocess has already crashed — return `ready=False` immediately with a message naming the exit code and the probe label. Exit-0 returns are ignored (the task detached cleanly, probe continues).

- [ ] **Step 1.1: Write failing tests**

Append to `tests/core/test_healthcheck.py`:

```python
# --- proc-poll fast-fail ---


def test_wait_proc_none_preserves_existing_behavior():
    """No proc passed → behave exactly as before (no poll calls)."""
    runner = HealthcheckRunner(MagicMock(spec=ShellRunner))
    hc = Healthcheck(sleep="10ms")  # unconditional-ready probe
    result = runner.wait(hc, Path("."), proc=None)
    assert result.ready


def test_wait_proc_still_running_keeps_probing():
    """proc.poll returns None throughout — probing continues normally."""
    # Start a TCP server the probe can connect to.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen(1)
    try:
        proc = MagicMock()
        proc.poll.return_value = None
        runner = HealthcheckRunner(MagicMock(spec=ShellRunner))
        hc = Healthcheck(
            tcp=f"127.0.0.1:{port}", timeout="2s", retry_interval="50ms",
        )
        result = runner.wait(hc, Path("."), proc=proc)
        assert result.ready
        # poll was called at least once
        assert proc.poll.call_count >= 1
    finally:
        server.close()


def test_wait_proc_crashed_nonzero_bails_immediately():
    """proc.poll returns non-zero → fast-fail without probing."""
    shell_mock = MagicMock(spec=ShellRunner)
    proc = MagicMock()
    proc.poll.return_value = 127
    runner = HealthcheckRunner(shell_mock)
    hc = Healthcheck(
        tcp="127.0.0.1:1", timeout="60s", retry_interval="500ms",
    )
    start = time.monotonic()
    result = runner.wait(hc, Path("."), proc=proc)
    elapsed = time.monotonic() - start

    assert not result.ready
    assert "exited with code 127" in result.message
    assert "tcp 127.0.0.1:1" in result.message
    assert elapsed < 1.0  # well under the 60s timeout
    # The tcp probe should NOT have attempted a real socket connection
    # (we never inject a listener). The shell shouldn't have been used either.
    shell_mock.run_task.assert_not_called()


def test_wait_proc_exit_zero_is_ignored():
    """Exit 0 means the task detached cleanly; probe must still decide."""
    proc = MagicMock()
    # First iteration: still running; second: cleanly exited; probe never passes.
    proc.poll.side_effect = [None, 0, 0, 0, 0, 0, 0, 0]
    runner = HealthcheckRunner(MagicMock(spec=ShellRunner))
    hc = Healthcheck(
        tcp="127.0.0.1:1", timeout="300ms", retry_interval="50ms",
    )
    result = runner.wait(hc, Path("."), proc=proc)
    assert not result.ready
    # The timeout path, NOT the fast-fail path — message format distinguishes.
    assert "timeout after" in result.message
    assert "exited with code" not in result.message


def test_wait_proc_delayed_crash_bails_after_a_few_iterations():
    """proc.poll: None for 2 iterations, then crashes with code 2."""
    proc = MagicMock()
    proc.poll.side_effect = [None, None, 2]
    runner = HealthcheckRunner(MagicMock(spec=ShellRunner))
    hc = Healthcheck(
        tcp="127.0.0.1:1", timeout="10s", retry_interval="50ms",
    )
    start = time.monotonic()
    result = runner.wait(hc, Path("."), proc=proc)
    elapsed = time.monotonic() - start

    assert not result.ready
    assert "exited with code 2" in result.message
    assert elapsed < 1.0  # caught within a few retry intervals, not 10s
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `pytest tests/core/test_healthcheck.py::test_wait_proc_crashed_nonzero_bails_immediately -v`
Expected: FAIL with `TypeError: HealthcheckRunner.wait() got an unexpected keyword argument 'proc'`.

- [ ] **Step 1.3: Add the `proc` parameter and poll check**

Edit `src/mship/core/healthcheck.py`.

Change the `wait` signature. Find:

```python
    def wait(
        self,
        healthcheck: Healthcheck,
        repo_path: Path,
        env_runner: str | None = None,
    ) -> HealthcheckResult:
```

Replace with:

```python
    def wait(
        self,
        healthcheck: Healthcheck,
        repo_path: Path,
        env_runner: str | None = None,
        proc: "subprocess.Popen | None" = None,
    ) -> HealthcheckResult:
```

Add the import at the top of the file if it's not already there. Find the imports block:

```python
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
```

Add:

```python
import subprocess
```

(The `subprocess.Popen` forward-reference in the `wait` signature needs the module imported. Keep it as a string annotation so the code doesn't require the import at call time, but importing the module also eliminates any runtime annotation-evaluation cost.)

Inside `wait()`, find the retry loop:

```python
        while True:
            elapsed = time.monotonic() - start

            if healthcheck.tcp is not None:
```

Insert immediately after `elapsed = time.monotonic() - start`:

```python
            # Fast-fail when the background Popen has crashed. Exit 0 is
            # ignored because many legitimate `run` tasks (e.g., `docker
            # run -d`) detach cleanly; the probe is the right signal for
            # those. Non-zero exit means the task itself died.
            if proc is not None:
                rc = proc.poll()
                if rc is not None and rc != 0:
                    return HealthcheckResult(
                        ready=False,
                        message=(
                            f"background process exited with code {rc} "
                            f"before {probe_label} passed"
                        ),
                        duration_s=elapsed,
                    )
```

- [ ] **Step 1.4: Run tests to verify they pass**

Run: `pytest tests/core/test_healthcheck.py -v`
Expected: all tests pass (5 new + existing).

- [ ] **Step 1.5: Commit**

```bash
git add src/mship/core/healthcheck.py tests/core/test_healthcheck.py
git commit -m "feat(healthcheck): fast-fail when background proc exits non-zero"
mship journal "HealthcheckRunner.wait polls optional proc each retry; non-zero exit bails immediately" --action committed
```

---

## Task 2: Executor threads Popen into healthcheck

**Files:**
- Modify: `src/mship/core/executor.py`

**Context:** The tier-execution loop already collects Popens into `tier_backgrounds`. We add a per-repo dict `repo_to_proc` populated alongside the list, then pass `proc=repo_to_proc.get(repo_result.repo)` into `self._healthcheck.wait(...)`.

- [ ] **Step 2.1: Modify the executor**

Edit `src/mship/core/executor.py`.

Find the tier loop body (inside `execute()`):

```python
        for tier in tiers:
            tier_results: list[RepoResult] = []
            tier_backgrounds: list = []

            if len(tier) == 1:
                # Single repo in tier — no threading overhead
                repo_result, bg = self._execute_one(tier[0], canonical_task, task_slug)
                tier_results.append(repo_result)
                if bg is not None:
                    tier_backgrounds.append(bg)
            else:
                # Multiple repos — run in parallel
                with ThreadPoolExecutor(max_workers=len(tier)) as pool:
                    futures = {
                        pool.submit(self._execute_one, repo_name, canonical_task, task_slug): repo_name
                        for repo_name in tier
                    }
                    for future in as_completed(futures):
                        repo_result, bg = future.result()
                        tier_results.append(repo_result)
                        if bg is not None:
                            tier_backgrounds.append(bg)
```

Replace with:

```python
        for tier in tiers:
            tier_results: list[RepoResult] = []
            tier_backgrounds: list = []
            # Map each background repo to its Popen so the healthcheck loop
            # (below) can poll the process and fast-fail on crash.
            repo_to_proc: dict[str, object] = {}

            if len(tier) == 1:
                # Single repo in tier — no threading overhead
                repo_result, bg = self._execute_one(tier[0], canonical_task, task_slug)
                tier_results.append(repo_result)
                if bg is not None:
                    tier_backgrounds.append(bg)
                    repo_to_proc[repo_result.repo] = bg
            else:
                # Multiple repos — run in parallel
                with ThreadPoolExecutor(max_workers=len(tier)) as pool:
                    futures = {
                        pool.submit(self._execute_one, repo_name, canonical_task, task_slug): repo_name
                        for repo_name in tier
                    }
                    for future in as_completed(futures):
                        repo_result, bg = future.result()
                        tier_results.append(repo_result)
                        if bg is not None:
                            tier_backgrounds.append(bg)
                            repo_to_proc[repo_result.repo] = bg
```

Then find the healthcheck call inside the same `execute()` method:

```python
                    hc_result = self._healthcheck.wait(
                        repo_config.healthcheck, cwd, env_runner
                    )
```

Replace with:

```python
                    hc_result = self._healthcheck.wait(
                        repo_config.healthcheck,
                        cwd,
                        env_runner,
                        proc=repo_to_proc.get(repo_result.repo),
                    )
```

- [ ] **Step 2.2: Run existing executor tests to verify no regression**

Run: `pytest tests/core/test_executor.py -v`
Expected: all existing tests still pass. The change adds an optional kwarg that defaults to None; foreground-run paths (which don't populate `repo_to_proc`) get `proc=None` via `.get()`, i.e., unchanged behavior.

If any test fails, the most likely culprits are:
- A test mocking `self._healthcheck.wait` with an exact argument assertion — update to accept the new `proc` kwarg.
- A test checking `repo_to_proc` directly (shouldn't exist; this is a new local).

- [ ] **Step 2.3: Run full core test subdir**

Run: `pytest tests/core/ -v`
Expected: all green.

- [ ] **Step 2.4: Commit**

```bash
git add src/mship/core/executor.py
git commit -m "feat(executor): pass background Popen into healthcheck for fast-fail"
mship journal "executor builds repo_to_proc map and threads proc into HealthcheckRunner.wait()" --action committed
```

---

## Task 3: Integration test — real crashing subprocess

**Files:**
- Modify: `tests/core/test_executor.py`

**Context:** End-to-end regression test that exercises the full wire: executor spawns a real Popen whose `run` command is `sh -c 'exit 127'`, healthcheck is configured to probe an unreachable port with a 60s timeout, and the whole pipeline should bail within a couple of seconds with the real exit code in the healthcheck message.

- [ ] **Step 3.1: Write the failing test**

Append to `tests/core/test_executor.py` (bottom of the file, after existing tests):

```python
import os as _os


@pytest.mark.skipif(_os.name == "nt", reason="Uses /bin/sh; Unix-only.")
def test_run_fast_fails_when_background_crashes(tmp_path):
    """A crashing background `run` task is caught during healthcheck
    retry rather than waiting the full healthcheck timeout.

    The service's `tasks.run` exits 127 immediately. Healthcheck timeout
    is 60s, retry_interval 200ms. Expectation: total elapsed well under
    2s, result marks the repo as failed, healthcheck message names the
    exit code.
    """
    import time as _time
    from pathlib import Path as _Path

    repo_dir = tmp_path / "svc"
    repo_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: fastfail
repos:
  svc:
    path: ./svc
    type: service
    tasks: {run: 'sh -c "exit 127"'}
    start_mode: background
    healthcheck:
      tcp: "127.0.0.1:1"
      timeout: 60s
      retry_interval: 200ms
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    # Use a real ShellRunner so run_streaming actually spawns the subprocess,
    # but override build_command to treat tasks["run"] as a literal shell cmd
    # (no `task` binary needed).
    from mship.util.shell import ShellRunner as _ShellRunner

    class _Shell(_ShellRunner):
        def build_command(self, command: str, env_runner: str | None = None) -> str:
            return command[len("task "):] if command.startswith("task ") else command

    from mship.core.healthcheck import HealthcheckRunner
    shell = _Shell()
    hc = HealthcheckRunner(shell)

    executor = RepoExecutor(config, graph, state_mgr, shell, healthcheck=hc)

    start = _time.monotonic()
    result = executor.execute("run", repos=["svc"])
    elapsed = _time.monotonic() - start

    assert elapsed < 2.0, f"expected fast-fail in under 2s, took {elapsed:.2f}s"
    assert not result.success
    assert result.results[0].shell_result.returncode == 1
    assert result.results[0].healthcheck is not None
    assert "exited with code 127" in result.results[0].healthcheck.message
```

Note: if `tests/core/test_executor.py` doesn't already import `ConfigLoader`, `DependencyGraph`, `StateManager`, `RepoExecutor`, add the imports at the top (they're very likely already there — scan before adding duplicates).

- [ ] **Step 3.2: Run test to verify the wiring**

Run: `pytest tests/core/test_executor.py::test_run_fast_fails_when_background_crashes -v`
Expected: PASS in under 2 seconds (the test asserts this).

If the test fails with `assert elapsed < 2.0` in the 60-second range, Task 2's wiring missed — check that the `proc=` kwarg is actually being passed through.

- [ ] **Step 3.3: Run the full executor + healthcheck test subdirs**

Run: `pytest tests/core/test_executor.py tests/core/test_healthcheck.py -v`
Expected: all green.

- [ ] **Step 3.4: Run full test suite**

Run: `pytest tests/`
Expected: green summary — all 864+ tests pass (current baseline from the PR #64 work).

- [ ] **Step 3.5: Commit**

```bash
git add tests/core/test_executor.py
git commit -m "test(executor): fast-fail integration test for background crash"
mship journal "end-to-end test: sh -c 'exit 127' caught in <2s instead of 60s timeout" --action committed
```

---

## Task 4: Manual smoke + finish PR

**Files:**
- None (verification only)

**Context:** Exercise the fix in a scratch workspace with a realistic background service config and confirm user-visible behavior.

- [ ] **Step 4.1: Reinstall tool, build scratch workspace**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/mship-run-fast-fails-when-background-service-crashes-during-healthcheck
uv tool install --reinstall --from . mothership

rm -rf /tmp/fastfail-smoke
mkdir -p /tmp/fastfail-smoke
cd /tmp/fastfail-smoke

cat > mothership.yaml <<'EOF'
workspace: fastfail-smoke
repos:
  crasher:
    path: ./crasher
    type: service
    tasks: {run: 'fake-binary-that-does-not-exist'}
    start_mode: background
    healthcheck:
      tcp: "127.0.0.1:8001"
      timeout: 60s
      retry_interval: 500ms
EOF

mkdir -p crasher .mothership
```

- [ ] **Step 4.2: Run it and measure elapsed time**

```bash
cd /tmp/fastfail-smoke
time mship run
```

Expected:
- `crasher` service is spawned, exits with code 127 (fake-binary not found).
- Within ~2 seconds, the healthcheck loop catches the crash.
- `crasher` shows a `crasher  | ...` prefixed error line via StreamPrinter.
- `ERROR: crasher: failed to start` printed at the end.
- `time`'s `real` line is well under 5 seconds (was 60+ before the fix).
- Exit code 1.

- [ ] **Step 4.3: Confirm healthy service still works**

```bash
cd /tmp/fastfail-smoke
cat > mothership.yaml <<'EOF'
workspace: fastfail-smoke
repos:
  happy:
    path: ./happy
    type: service
    tasks: {run: 'python3 -c "import http.server,socketserver; h=socketserver.TCPServer((\"127.0.0.1\",8765), http.server.SimpleHTTPRequestHandler); h.serve_forever()"'}
    start_mode: background
    healthcheck:
      tcp: "127.0.0.1:8765"
      timeout: 10s
      retry_interval: 200ms
EOF
mkdir -p happy
timeout 3 mship run; echo "EXIT: $?"
```

Expected:
- Service starts, healthcheck tcp :8765 passes quickly.
- `mship run` proceeds past healthcheck (prints "Press Ctrl-C to stop" or similar).
- `timeout 3` kills it after 3 seconds → `EXIT: 124`.
- (The key signal: no fast-fail firing on a healthy service.)

- [ ] **Step 4.4: Cleanup**

```bash
rm -rf /tmp/fastfail-smoke
```

- [ ] **Step 4.5: Full pytest final check**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/mship-run-fast-fails-when-background-service-crashes-during-healthcheck
pytest tests/ 2>&1 | tail -3
```

Expected: green summary, 865+ tests pass.

- [ ] **Step 4.6: Open PR**

Write this body to `/tmp/fastfail-body.md`:

```markdown
## Summary

When a background service's `run` Popen crashes during startup (e.g., missing binary, syntax error), `mship run` now detects the exit within one healthcheck retry interval and surfaces the real exit code — instead of waiting the full healthcheck timeout (previously 60s in realistic configs).

Real-world scenario from the `tailrd` workspace: `tailrd`'s `dev` task runs `infra:ensure` which calls the `aws` CLI. If `aws` isn't installed, the task exits 127 immediately. Before this change, mship waited 60s for the tailrd healthcheck to time out before reporting the failure. After: mship reports `"background process exited with code 127 before http http://127.0.0.1:8000/health passed"` within a second.

## Scope

- Applies only to repos with `start_mode: background` AND a `healthcheck` block (the ones that enter the healthcheck retry loop).
- Exit 0 is intentionally ignored — many `run` tasks cleanly detach (`docker run -d`, `systemctl start`) and exit 0 while the actual service runs in the background. Healthcheck keeps probing.
- No change to foreground `run` (already uses `popen.wait()` directly), no change to non-`run` tasks, no new CLI flags.

## Changes

- `src/mship/core/healthcheck.py` — `HealthcheckRunner.wait()` gains an optional `proc: subprocess.Popen | None` parameter. Inside the retry loop, if `proc.poll()` returns non-zero, return `ready=False` immediately with a message naming the exit code and the probe label.
- `src/mship/core/executor.py` — `execute()` builds a `repo_to_proc: dict[str, Popen]` map during tier execution and passes the matching Popen into `self._healthcheck.wait(...)`.

## Test plan

- [x] `tests/core/test_healthcheck.py`: 5 new unit tests (proc=None preserves behavior; live proc keeps probing; non-zero exit bails immediately with elapsed < 1s; exit-0 is ignored; delayed crash caught within a few iterations).
- [x] `tests/core/test_executor.py`: 1 new integration test. Real subprocess `sh -c 'exit 127'` with a 60s healthcheck timeout is caught in under 2 seconds; shell_result.returncode==1; healthcheck message contains "exited with code 127".
- [x] Full suite: 865+ passed.
- [x] Manual smoke: crashing service exits `mship run` in ~2s (was 60s); healthy service still passes healthcheck normally.
```

Then:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/mship-run-fast-fails-when-background-service-crashes-during-healthcheck
mship finish --body-file /tmp/fastfail-body.md
```

Expected: PR URL returned.

---

## Done when

- [x] `HealthcheckRunner.wait(proc=...)` polls the Popen every retry; non-zero exit returns fast-fail with a descriptive message.
- [x] Exit 0 is ignored — probing continues.
- [x] Executor builds `repo_to_proc` and threads it into each `wait()` call; missing Popens pass `proc=None`.
- [x] Foreground run and non-`run` tasks unchanged.
- [x] All existing tests pass; 5 new healthcheck unit tests + 1 integration test pass.
- [x] Manual smoke confirms `mship run` exits within seconds for a crashing service, and healthy services still start normally.

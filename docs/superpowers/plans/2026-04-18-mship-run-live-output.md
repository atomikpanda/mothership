# `mship run` Live Prefixed Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `mship run` stream every service's stdout and stderr to our stdout in real time, prefixed with the repo name, matching docker-compose convention.

**Architecture:** A new `StreamPrinter` helper holds per-repo colors + shared stdout lock. Drain threads read subprocess PIPEs line-by-line and hand each line to the printer. The executor wires both the foreground `run` branch (previously capture-and-discard) and the background `run` branch (previously unread PIPEs) through this path, scoped to `canonical_task == "run"`.

**Tech Stack:** Python 3.14, `subprocess.Popen`, `threading.Thread`, stdlib `sys.stdout` + raw ANSI escape codes (no Rich dependency in the new helper).

**Reference spec:** `docs/superpowers/specs/2026-04-18-mship-run-live-output-design.md`

---

## File structure

**New files:**
- `src/mship/util/stream_printer.py` — `StreamPrinter` class, `drain_to_printer()` helper, color palette + ANSI helper. One module, one responsibility: prefix service output and serialize writes.
- `tests/util/test_stream_printer.py` — unit tests for `StreamPrinter` and the ANSI/color helpers.
- `tests/util/test_shell_streaming.py` — unit tests for `drain_to_printer()` against a fake `Popen`.
- `tests/core/test_executor_run_streaming.py` — integration tests with real `Popen` subprocesses through the full executor.

**Modified files:**
- `src/mship/core/executor.py` — `execute()` constructs a `StreamPrinter` when `canonical_task == "run"`; `_execute_one` routes both foreground and background `run` through `Popen` + `drain_to_printer`.

**Unchanged files (expected to stay green):**
- `src/mship/cli/exec.py` — CLI `run_cmd` wait-loop, signal forwarding, kill-group logic is all reused unchanged.
- `src/mship/util/shell.py` — `run_streaming`, `run`, `run_task` stay as-is. The new helpers go in a sibling module so `shell.py` keeps its single responsibility (subprocess plumbing, no display logic).

**Task ordering rationale:** Task 1 (StreamPrinter) is pure and testable without subprocesses. Task 2 (drain_to_printer) depends on Task 1 and can be tested with a fake Popen. Task 3 wires the executor end-to-end. Task 4 runs real subprocesses through the whole stack. Task 5 manual smoke + PR.

---

## Task 1: StreamPrinter module

**Files:**
- Create: `src/mship/util/stream_printer.py`
- Create: `tests/util/test_stream_printer.py`

**Context:** Pure module. `StreamPrinter` formats and prints prefixed lines under a shared lock. Color is applied via raw ANSI when attached to a TTY, skipped otherwise. Width is computed at construction from the longest repo name in the run.

- [ ] **Step 1.1: Write failing tests**

Write `tests/util/test_stream_printer.py`:

```python
import io
import re
import sys
import threading

import pytest

from mship.util.stream_printer import StreamPrinter, _assign_colors


def _drain_capsys(capsys):
    """Read the combined captured text. Works under capsys and capfd."""
    return capsys.readouterr().out


def test_write_pads_repo_to_longest_width(capsys):
    p = StreamPrinter(repos=["api", "worker"], use_color=False)
    p.write("api", "hello\n")
    p.write("worker", "world\n")
    out = _drain_capsys(capsys)
    assert "api     | hello\n" in out     # 3 chars + 3 pad + "  | "
    assert "worker  | world\n" in out     # 6 chars + 0 pad + "  | "


def test_write_with_single_repo(capsys):
    p = StreamPrinter(repos=["only"], use_color=False)
    p.write("only", "line\n")
    out = _drain_capsys(capsys)
    assert "only  | line\n" in out


def test_write_empty_repo_list(capsys):
    """Edge case: width=0 still produces a parseable prefix."""
    p = StreamPrinter(repos=[], use_color=False)
    p.write("x", "z\n")
    out = _drain_capsys(capsys)
    assert "x  | z\n" in out


def test_write_strips_trailing_newlines_but_keeps_inner(capsys):
    p = StreamPrinter(repos=["api"], use_color=False)
    p.write("api", "line1\n")
    p.write("api", "line2")          # no trailing newline
    p.write("api", "line3\r\n")      # CRLF
    out = _drain_capsys(capsys)
    assert "api  | line1\n" in out
    assert "api  | line2\n" in out
    assert "api  | line3\n" in out


def test_use_color_true_adds_ansi(capsys):
    p = StreamPrinter(repos=["api"], use_color=True)
    p.write("api", "hello\n")
    out = _drain_capsys(capsys)
    assert "\x1b[" in out             # ANSI CSI present
    assert "hello" in out


def test_use_color_false_no_ansi(capsys):
    p = StreamPrinter(repos=["api"], use_color=False)
    p.write("api", "hello\n")
    out = _drain_capsys(capsys)
    assert "\x1b[" not in out


def test_use_color_auto_detects_isatty(capsys, monkeypatch):
    """When use_color is unset, default to sys.stdout.isatty()."""
    # capsys makes stdout non-tty; auto-detection should disable color.
    p = StreamPrinter(repos=["api"])
    p.write("api", "hello\n")
    out = _drain_capsys(capsys)
    assert "\x1b[" not in out


def test_assign_colors_deterministic():
    """Same repo list in any order produces the same repo->color mapping."""
    c1 = _assign_colors(["b", "a", "c"])
    c2 = _assign_colors(["a", "c", "b"])
    assert c1 == c2
    # Three repos → three distinct colors
    assert len({c1["a"], c1["b"], c1["c"]}) == 3


def test_assign_colors_cycles_palette_beyond_six_repos():
    """Palette is 6 colors; 8 repos should cycle without crashing."""
    repos = [f"r{i}" for i in range(8)]
    colors = _assign_colors(repos)
    assert len(colors) == 8
    assert all(isinstance(c, str) for c in colors.values())


def test_thread_safety_no_line_tearing(capsys):
    """10 threads × 100 writes each = 1000 lines. Every captured line
    must match the valid prefix pattern; no mid-line interleaving."""
    p = StreamPrinter(repos=["api", "worker"], use_color=False)

    def _writer(repo, n):
        for i in range(n):
            p.write(repo, f"line-{i}-{repo}\n")

    threads = [
        threading.Thread(target=_writer, args=("api", 500)),
        threading.Thread(target=_writer, args=("worker", 500)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    out = _drain_capsys(capsys)
    pattern = re.compile(r"^(api|worker)\s*\| line-\d+-(api|worker)$")
    non_empty = [ln for ln in out.splitlines() if ln]
    assert len(non_empty) == 1000
    for ln in non_empty:
        m = pattern.match(ln)
        assert m is not None, f"line did not match: {ln!r}"
        # repo name in prefix must match repo name in content
        assert m.group(1) == m.group(2), f"prefix/content mismatch: {ln!r}"
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `pytest tests/util/test_stream_printer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mship.util.stream_printer'`.

- [ ] **Step 1.3: Create the module**

Write `src/mship/util/stream_printer.py`:

```python
"""Line-prefixed, thread-safe printer for `mship run` service output.

Constructed once per `mship run` invocation. Drain threads (one per
service's stdout and stderr PIPE) call `write()` with each line as it
arrives. The printer pads the repo name to a fixed width, applies an
ANSI color when attached to a TTY, acquires a lock, and writes to
stdout so that lines from parallel services never tear.

This module intentionally has no Rich dependency: it emits raw ANSI
escape codes so the output is deterministic and easy to test.
"""
from __future__ import annotations

import sys
import threading


# Fixed palette, cycled in sorted-repo order for deterministic coloring.
_PALETTE = ("36", "32", "33", "35", "34", "31")  # cyan, green, yellow, magenta, blue, red


def _assign_colors(repos: list[str]) -> dict[str, str]:
    """Return {repo: ansi_color_code} cycling _PALETTE in sorted order."""
    return {
        repo: _PALETTE[i % len(_PALETTE)]
        for i, repo in enumerate(sorted(repos))
    }


def _colorize(text: str, ansi_code: str) -> str:
    """Wrap `text` in an ANSI SGR escape sequence."""
    return f"\x1b[{ansi_code}m{text}\x1b[0m"


class StreamPrinter:
    """Thread-safe line-prefixed printer for multi-service output."""

    def __init__(self, repos: list[str], use_color: bool | None = None) -> None:
        self._width = max((len(r) for r in repos), default=0)
        self._colors = _assign_colors(repos)
        self._use_color = (
            sys.stdout.isatty() if use_color is None else use_color
        )
        self._lock = threading.Lock()

    def write(self, repo: str, line: str) -> None:
        # Normalise trailing whitespace: strip any \r\n combinations and
        # re-add exactly one \n so the output is consistent regardless of
        # what the child process emits.
        content = line.rstrip("\r\n")
        prefix = f"{repo:<{self._width}}  | "
        if self._use_color and repo in self._colors:
            prefix = _colorize(prefix, self._colors[repo])
        with self._lock:
            sys.stdout.write(f"{prefix}{content}\n")
            sys.stdout.flush()
```

- [ ] **Step 1.4: Run tests to verify they pass**

Run: `pytest tests/util/test_stream_printer.py -v`
Expected: all 10 tests pass.

- [ ] **Step 1.5: Commit (pair with `mship journal`)**

```bash
git add src/mship/util/stream_printer.py tests/util/test_stream_printer.py
git commit -m "feat(util): StreamPrinter — thread-safe prefixed line printer"
mship journal "StreamPrinter with per-repo color, width padding, thread-safe writes; 10 unit tests" --action committed
```

---

## Task 2: `drain_to_printer` helper

**Files:**
- Modify: `src/mship/util/stream_printer.py`
- Create: `tests/util/test_shell_streaming.py`

**Context:** `drain_to_printer(proc, repo, printer)` spawns two daemon threads that call `proc.stdout.readline()` and `proc.stderr.readline()` in a loop, feeding each line into the printer. Returns the threads so callers can `join()` them after `proc.wait()`.

- [ ] **Step 2.1: Write failing tests**

Write `tests/util/test_shell_streaming.py`:

```python
import io
import threading
import time

import pytest

from mship.util.stream_printer import StreamPrinter, drain_to_printer


class _FakePopen:
    """Minimal Popen-shaped object for drain tests."""
    def __init__(self, stdout_text: str = "", stderr_text: str = ""):
        self.stdout = io.StringIO(stdout_text) if stdout_text else None
        self.stderr = io.StringIO(stderr_text) if stderr_text else None


def test_drain_prints_stdout_lines(capsys):
    printer = StreamPrinter(repos=["api"], use_color=False)
    proc = _FakePopen(stdout_text="line1\nline2\n")
    threads = drain_to_printer(proc, "api", printer)
    for t in threads:
        t.join(timeout=1.0)
    out = capsys.readouterr().out
    assert "api  | line1\n" in out
    assert "api  | line2\n" in out


def test_drain_prints_stderr_lines(capsys):
    printer = StreamPrinter(repos=["api"], use_color=False)
    proc = _FakePopen(stderr_text="err-line\n")
    threads = drain_to_printer(proc, "api", printer)
    for t in threads:
        t.join(timeout=1.0)
    out = capsys.readouterr().out
    assert "api  | err-line\n" in out


def test_drain_handles_none_streams(capsys):
    """If proc.stdout or proc.stderr is None, drain should not error."""
    printer = StreamPrinter(repos=["api"], use_color=False)
    proc = _FakePopen()  # both streams None
    threads = drain_to_printer(proc, "api", printer)
    for t in threads:
        t.join(timeout=1.0)
    # No output, no exceptions
    assert capsys.readouterr().out == ""


def test_drain_prefixes_are_correct_for_multiple_repos(capsys):
    """Two separate drain invocations writing to same printer — both
    lines appear with correct prefixes, no tearing."""
    printer = StreamPrinter(repos=["api", "worker"], use_color=False)
    p1 = _FakePopen(stdout_text="hello from api\n")
    p2 = _FakePopen(stdout_text="hello from worker\n")
    threads = drain_to_printer(p1, "api", printer) + drain_to_printer(p2, "worker", printer)
    for t in threads:
        t.join(timeout=1.0)
    out = capsys.readouterr().out
    assert "api     | hello from api\n" in out
    assert "worker  | hello from worker\n" in out


def test_drain_returns_two_threads():
    printer = StreamPrinter(repos=["api"], use_color=False)
    proc = _FakePopen(stdout_text="a\n", stderr_text="b\n")
    threads = drain_to_printer(proc, "api", printer)
    assert len(threads) == 2
    for t in threads:
        t.join(timeout=1.0)


def test_drain_threads_are_daemons():
    printer = StreamPrinter(repos=["api"], use_color=False)
    proc = _FakePopen(stdout_text="a\n")
    threads = drain_to_printer(proc, "api", printer)
    for t in threads:
        assert t.daemon is True
        t.join(timeout=1.0)
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `pytest tests/util/test_shell_streaming.py -v`
Expected: FAIL with `ImportError: cannot import name 'drain_to_printer' from 'mship.util.stream_printer'`.

- [ ] **Step 2.3: Add `drain_to_printer` to the module**

Append to `src/mship/util/stream_printer.py`:

```python
def drain_to_printer(
    proc,
    repo: str,
    printer: StreamPrinter,
) -> list[threading.Thread]:
    """Start daemon threads that read proc.stdout and proc.stderr and
    feed every line to `printer`. Returns the threads so the caller can
    join() them after proc.wait() if it wants to ensure all output has
    flushed before continuing.

    If either stream is None (e.g. caller didn't request a PIPE) the
    corresponding thread exits immediately without error.
    """
    def _drain(stream):
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                printer.write(repo, line)
        except Exception:
            # Reading from a closed/broken stream: exit cleanly. The
            # main thread will observe the process exit via proc.wait().
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    threads = [
        threading.Thread(target=_drain, args=(proc.stdout,), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr,), daemon=True),
    ]
    for t in threads:
        t.start()
    return threads
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `pytest tests/util/test_shell_streaming.py -v`
Expected: 6 passed.

Run: `pytest tests/util/ -v`
Expected: all tests in tests/util/ still pass (including the existing `test_shell.py` tests).

- [ ] **Step 2.5: Commit**

```bash
git add src/mship/util/stream_printer.py tests/util/test_shell_streaming.py
git commit -m "feat(util): drain_to_printer — daemon threads relay Popen output"
mship journal "drain_to_printer helper + 6 unit tests for Popen-shaped streams" --action committed
```

---

## Task 3: Wire executor — foreground and background `run` both stream

**Files:**
- Modify: `src/mship/core/executor.py`

**Context:** The executor has two paths in `_execute_one` that need to change when `canonical_task == "run"`:

1. **Background (`start_mode == "background"` AND canonical is `run`)** — already uses `Popen` via `run_streaming`. Add one line: attach `drain_to_printer` so output actually appears.
2. **Foreground (anything else with canonical `run`)** — currently calls `run_task()` which uses `subprocess.run(capture_output=True)`. Swap to `Popen` via `run_streaming`, attach drain threads, wait for completion, join drain threads.

In `execute()`, construct the `StreamPrinter` once per call when the canonical task is `run`. Hold as `self._printer` so `_execute_one` can reach it. For non-`run` tasks, leave `self._printer = None`.

No test in this task — existing tests stay green; Task 4 adds new integration tests.

- [ ] **Step 3.1: Run existing executor tests to establish baseline**

Run: `pytest tests/core/test_executor.py -v`
Expected: all tests pass. Record the pass count for after-comparison.

- [ ] **Step 3.2: Modify the executor**

Edit `src/mship/core/executor.py`:

Add two imports near the top (after the existing `from mship.util.shell import ...` line):

```python
from mship.util.stream_printer import StreamPrinter, drain_to_printer
```

In the `RepoExecutor` class, extend `__init__` to initialise `self._printer`:

Find:
```python
    def __init__(
        self,
        config: WorkspaceConfig,
        graph: DependencyGraph,
        state_manager: StateManager,
        shell: ShellRunner,
        healthcheck,  # HealthcheckRunner
    ) -> None:
        self._config = config
        self._graph = graph
        self._state_manager = state_manager
        self._shell = shell
        self._healthcheck = healthcheck
```

Append after `self._healthcheck = healthcheck`:

```python
        self._printer: StreamPrinter | None = None
```

Replace the existing `_execute_one` method with:

```python
    def _execute_one(
        self,
        repo_name: str,
        canonical_task: str,
        task_slug: str | None,
    ) -> tuple[RepoResult, object | None]:
        """Execute a single repo's task. Thread-safe.

        Returns (RepoResult, background_process_or_None).
        """
        actual_name = self.resolve_task_name(repo_name, canonical_task)
        env_runner = self.resolve_env_runner(repo_name)
        upstream_env = self.resolve_upstream_env(repo_name, task_slug)
        cwd = self._resolve_cwd(repo_name, task_slug)
        repo_config = self._config.repos[repo_name]

        if repo_config.start_mode == "background" and canonical_task == "run":
            # Launch as background subprocess, don't wait
            command = self._shell.build_command(
                f"task {actual_name}", env_runner
            )
            popen = self._shell.run_streaming(command, cwd=cwd)
            # Drain stdout/stderr to the shared printer. Threads are daemon
            # and die naturally when the PIPEs close at process exit.
            if self._printer is not None:
                drain_to_printer(popen, repo_name, self._printer)
            return (
                RepoResult(
                    repo=repo_name,
                    task_name=actual_name,
                    shell_result=ShellResult(returncode=0, stdout="", stderr=""),
                    background_pid=popen.pid,
                ),
                popen,
            )

        if canonical_task == "run":
            # Foreground `run` task: stream output live via Popen + drain
            # threads, then wait for completion. This replaces the old
            # capture-and-never-print behavior of run_task().
            command = self._shell.build_command(
                f"task {actual_name}", env_runner
            )
            _start = _time.monotonic()
            popen = self._shell.run_streaming(command, cwd=cwd)
            threads: list = []
            if self._printer is not None:
                threads = drain_to_printer(popen, repo_name, self._printer)
            returncode = popen.wait()
            for t in threads:
                t.join(timeout=1.0)
            _elapsed_ms = int((_time.monotonic() - _start) * 1000)
            return (
                RepoResult(
                    repo=repo_name,
                    task_name=actual_name,
                    # Output already streamed to stdout; the ShellResult
                    # carries only the returncode for downstream logic.
                    shell_result=ShellResult(returncode=returncode, stdout="", stderr=""),
                    duration_ms=_elapsed_ms,
                ),
                None,
            )

        # Non-run tasks (setup, test, ...) keep the capture-and-return path.
        _start = _time.monotonic()
        shell_result = self._shell.run_task(
            task_name=canonical_task,
            actual_task_name=actual_name,
            cwd=cwd,
            env_runner=env_runner,
            env=upstream_env or None,
        )
        _elapsed_ms = int((_time.monotonic() - _start) * 1000)

        return (
            RepoResult(
                repo=repo_name,
                task_name=actual_name,
                shell_result=shell_result,
                duration_ms=_elapsed_ms,
            ),
            None,
        )
```

Note: the background branch's `ShellResult(returncode=0, ...)` is unchanged from the existing code — we trust Popen spawned successfully. The foreground `run` branch's `upstream_env` is not threaded through `run_streaming`; see Step 3.3 for why this is OK for v1.

Replace the existing `execute()` method to construct the printer:

Find the start of `execute`:
```python
    def execute(
        self,
        canonical_task: str,
        repos: list[str],
        run_all: bool = False,
        task_slug: str | None = None,
    ) -> ExecutionResult:
        tiers = self._graph.topo_tiers(repos)
        result = ExecutionResult()
```

Insert a new block immediately after `result = ExecutionResult()`:

```python
        if canonical_task == "run":
            self._printer = StreamPrinter(repos=sorted(set(repos)))
        else:
            self._printer = None
```

(The rest of `execute()` stays unchanged.)

- [ ] **Step 3.3: Handle upstream_env in foreground-run path**

`run_streaming(command, cwd)` today does not accept an `env` argument. Foreground `run` tasks have `upstream_env` set when the repo has `depends_on` with worktree paths. To keep the foreground run path equivalent to the previous capture-output path, either:

1. Thread `env` through `run_streaming`, or
2. Inline the Popen call in the executor (bypassing `run_streaming`) with env wired in.

Choose option 1 — it's a tiny change and keeps `run_streaming` honest.

Edit `src/mship/util/shell.py`. Change the signature of `run_streaming`:

Find:
```python
    def run_streaming(self, command: str, cwd: Path) -> subprocess.Popen:
        """Run a command with stdout/stderr streaming (for logs, run).

        Launches the subprocess in its own process group so signal delivery
        can reach the whole tree (including grandchildren) on termination.
        """
        kwargs = dict(
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(command, **kwargs)
```

Replace with:
```python
    def run_streaming(
        self,
        command: str,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen:
        """Run a command with stdout/stderr streaming (for logs, run).

        Launches the subprocess in its own process group so signal delivery
        can reach the whole tree (including grandchildren) on termination.
        """
        run_env = None
        if env:
            run_env = {**os.environ, **env}
        kwargs = dict(
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=run_env,
        )
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(command, **kwargs)
```

Then update the foreground-run branch in `_execute_one` (Step 3.2) to pass `env`:

Find (inside the `if canonical_task == "run":` branch you just wrote):
```python
            popen = self._shell.run_streaming(command, cwd=cwd)
```

Replace with:
```python
            popen = self._shell.run_streaming(command, cwd=cwd, env=upstream_env or None)
```

(Do NOT change the background-run branch. Background tasks don't use upstream_env today, and changing that is out of scope.)

- [ ] **Step 3.4: Run the existing executor tests**

Run: `pytest tests/core/test_executor.py tests/util/test_shell.py -v`
Expected: all existing tests still pass. The executor change only affects `canonical_task == "run"` paths; setup/test/other tasks unaffected. The `run_streaming` signature change is additive (env defaults to None).

If any test fails, fix the mismatch. Candidates for breakage:
- A test that passes positional args to `run_streaming` — unlikely, but check.
- A test that mocks `subprocess.Popen` and asserts on the kwarg dict — it will now also see `env=None` added. Update the assertion to not require an exact-match dict.

- [ ] **Step 3.5: Commit**

```bash
git add src/mship/core/executor.py src/mship/util/shell.py
git commit -m "feat(executor): stream run-task output via StreamPrinter"
mship journal "executor wires foreground + background run through drain_to_printer; run_streaming gets env kwarg" --action committed
```

---

## Task 4: Integration test with real subprocesses

**Files:**
- Create: `tests/core/test_executor_run_streaming.py`

**Context:** End-to-end test that runs real `sh -c '...'` subprocesses through the full executor and asserts the streamed output appears in pytest's `capsys`. This is the test that catches regressions in the integration wiring between executor, StreamPrinter, and drain threads.

- [ ] **Step 4.1: Write the test file**

Write `tests/core/test_executor_run_streaming.py`:

```python
"""Integration tests for `canonical_task == "run"` streaming.

These spin up real subprocesses and assert on captured stdout. They
use shell.run_streaming via the real executor — no mocks on the
subprocess layer.
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.executor import RepoExecutor
from mship.core.graph import DependencyGraph
from mship.util.shell import ShellRunner


def _build_executor(tmp_path: Path, repo_commands: dict[str, str]) -> RepoExecutor:
    """Build a real RepoExecutor over a minimal config. Each repo's `run`
    task is set to an inline shell command — no Taskfile indirection.
    repo_commands maps repo name to the shell command string to run."""
    repos: dict[str, RepoConfig] = {}
    for name, cmd in repo_commands.items():
        repo_dir = tmp_path / name
        repo_dir.mkdir()
        repos[name] = RepoConfig(
            path=repo_dir,
            type="service",
            tasks={"run": cmd},
            start_mode="foreground",
        )
    config = WorkspaceConfig(workspace="t", repos=repos)
    graph = DependencyGraph(config)
    state_mgr = MagicMock()
    state_mgr.load.return_value = MagicMock(tasks={})

    class _Shell(ShellRunner):
        """Override build_command so we don't need `task` CLI installed.
        The test sends raw shell strings through run / run_streaming."""
        def build_command(self, command: str, env_runner: str | None = None) -> str:
            # command is "task <actual-name>" where actual-name is the raw
            # command we stashed in tasks["run"]. Strip the `task ` prefix
            # and execute the command directly.
            if command.startswith("task "):
                return command[len("task "):]
            return command

    return RepoExecutor(
        config=config,
        graph=graph,
        state_manager=state_mgr,
        shell=_Shell(),
        healthcheck=MagicMock(wait=lambda *a, **kw: MagicMock(ready=True, message="")),
    )


def test_foreground_run_streams_stdout_and_stderr(capsys, tmp_path):
    ex = _build_executor(tmp_path, {
        "api": "sh -c 'echo hello; echo world >&2; exit 0'",
    })
    result = ex.execute("run", repos=["api"])
    out = capsys.readouterr().out
    assert "api  | hello" in out
    assert "api  | world" in out
    assert result.success is True


def test_foreground_run_nonzero_returncode_still_streams(capsys, tmp_path):
    ex = _build_executor(tmp_path, {
        "api": "sh -c 'echo oops; exit 2'",
    })
    result = ex.execute("run", repos=["api"])
    out = capsys.readouterr().out
    assert "api  | oops" in out
    assert result.success is False
    assert result.results[0].shell_result.returncode == 2


def test_two_parallel_services_both_visible(capsys, tmp_path):
    ex = _build_executor(tmp_path, {
        "api":    "sh -c 'echo from-api; sleep 0.05; echo api-done'",
        "worker": "sh -c 'echo from-worker; sleep 0.05; echo worker-done'",
    })
    result = ex.execute("run", repos=["api", "worker"])
    out = capsys.readouterr().out
    assert "api     | from-api" in out
    assert "api     | api-done" in out
    assert "worker  | from-worker" in out
    assert "worker  | worker-done" in out
    assert result.success is True


def test_background_run_streams_output(capsys, tmp_path):
    """`start_mode: background` services also get their output relayed."""
    repo_dir = tmp_path / "bg"
    repo_dir.mkdir()
    config = WorkspaceConfig(
        workspace="t",
        repos={
            "bg": RepoConfig(
                path=repo_dir,
                type="service",
                tasks={"run": "sh -c 'echo bg-hello; sleep 0.1'"},
                start_mode="background",
            ),
        },
    )
    graph = DependencyGraph(config)
    state_mgr = MagicMock()
    state_mgr.load.return_value = MagicMock(tasks={})

    class _Shell(ShellRunner):
        def build_command(self, command: str, env_runner: str | None = None) -> str:
            return command[len("task "):] if command.startswith("task ") else command

    ex = RepoExecutor(
        config=config,
        graph=graph,
        state_manager=state_mgr,
        shell=_Shell(),
        healthcheck=MagicMock(wait=lambda *a, **kw: MagicMock(ready=True, message="")),
    )
    result = ex.execute("run", repos=["bg"])
    # Background subprocess is still alive here; wait for it to finish so
    # drain threads fully relay the output before we assert.
    for proc in result.background_processes:
        proc.wait(timeout=2)
    # Give drain threads a moment to flush.
    import time as _t
    _t.sleep(0.1)
    out = capsys.readouterr().out
    assert "bg  | bg-hello" in out


def test_non_run_task_does_not_stream(capsys, tmp_path):
    """Setup/test/etc stay on the capture path — output should NOT appear
    on our stdout via the printer."""
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    config = WorkspaceConfig(
        workspace="t",
        repos={
            "r": RepoConfig(
                path=repo_dir,
                type="service",
                tasks={"setup": "echo setup-output"},
                start_mode="foreground",
            ),
        },
    )
    graph = DependencyGraph(config)
    state_mgr = MagicMock()
    state_mgr.load.return_value = MagicMock(tasks={})

    class _Shell(ShellRunner):
        def build_command(self, command: str, env_runner: str | None = None) -> str:
            return command[len("task "):] if command.startswith("task ") else command

    ex = RepoExecutor(
        config=config,
        graph=graph,
        state_manager=state_mgr,
        shell=_Shell(),
        healthcheck=MagicMock(wait=lambda *a, **kw: MagicMock(ready=True, message="")),
    )
    ex.execute("setup", repos=["r"])
    out = capsys.readouterr().out
    # setup-output should NOT appear as a prefixed line — setup uses
    # capture path, returning the string in ShellResult.stdout instead.
    assert "r  | setup-output" not in out
```

- [ ] **Step 4.2: Run integration tests**

Run: `pytest tests/core/test_executor_run_streaming.py -v`
Expected: 5 passed.

If any test fails with a config shape issue (e.g., `RepoConfig` requires fields you didn't provide), inspect `src/mship/core/config.py` for the required defaults and fix the test fixture. Don't work around by modifying the executor — the config shape is the source of truth.

- [ ] **Step 4.3: Run the full executor test subdir**

Run: `pytest tests/core/test_executor.py tests/core/test_executor_run_streaming.py -v`
Expected: all green.

- [ ] **Step 4.4: Run full test suite (excluding known-flaky)**

Run: `pytest tests/ --ignore=tests/core/view/test_web_port.py 2>&1 | tail -20`
Expected: a passing summary. No regressions in existing tests.

- [ ] **Step 4.5: Commit**

```bash
git add tests/core/test_executor_run_streaming.py
git commit -m "test(executor): integration tests for run streaming"
mship journal "5 integration tests covering foreground + background + parallel + non-run" --action committed
```

---

## Task 5: Manual smoke + finish PR

**Files:**
- None (verification only)

**Context:** Exercise the feature end-to-end with real `task` CLI commands and two repos.

- [ ] **Step 5.1: Reinstall tool, build scratch workspace**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/mship-run-shows-prefixed-live-stdout-and-stderr
uv tool install --reinstall --from . mothership
rm -rf /tmp/run-stream-smoke
mkdir -p /tmp/run-stream-smoke
cd /tmp/run-stream-smoke

cat > mothership.yaml <<'EOF'
workspace: run-stream-smoke
repos:
  api:
    path: ./api
    type: service
  worker:
    path: ./worker
    type: service
EOF

mkdir -p api worker
for r in api worker; do
  cat > "$r/Taskfile.yml" <<EOF
version: '3'
tasks:
  run:
    cmds:
      - for i in 1 2 3 4 5; do echo "[$r] line \$i"; sleep 0.2; done
      - echo "$r done" >&2
EOF
done
mkdir -p .mothership
```

- [ ] **Step 5.2: Interleaved foreground streaming**

```bash
cd /tmp/run-stream-smoke
mship run 2>&1 | tee /tmp/run-stream-smoke.out
```

Expected:
- Lines from both `api` and `worker` interleave in real time (not a burst at the end).
- Each line is prefixed: `api     | [api] line 1`, `worker  | [worker] line 1`, etc.
- The stderr line ("api done", "worker done") also appears prefixed.
- Exit 0 at the end.
- With the `| tee` pipe, ANSI colors should NOT appear (non-TTY destination).

- [ ] **Step 5.3: TTY smoke (color on)**

```bash
cd /tmp/run-stream-smoke
mship run
```

Expected:
- Same lines, now with color per-repo prefix (cyan for api, green for worker — whichever the deterministic palette assignment produces in sorted order).
- Exit 0 at the end.

- [ ] **Step 5.4: Non-zero exit propagates**

Edit api/Taskfile.yml run task to exit 1 at the end:

```bash
cd /tmp/run-stream-smoke
cat > api/Taskfile.yml <<'EOF'
version: '3'
tasks:
  run:
    cmds:
      - echo "api starting"
      - echo "api oopsing" >&2
      - exit 1
EOF

mship run; echo "EXIT: $?"
```

Expected:
- `api  | api starting` and `api  | api oopsing` appear (merged stderr with same prefix).
- `worker` output also appears.
- `api: failed to start` summary line (existing CLI behavior).
- `EXIT: 1`.

- [ ] **Step 5.5: Cleanup**

```bash
rm -rf /tmp/run-stream-smoke /tmp/run-stream-smoke.out
```

- [ ] **Step 5.6: Full pytest final check**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/mship-run-shows-prefixed-live-stdout-and-stderr
pytest tests/ --ignore=tests/core/view/test_web_port.py 2>&1 | tail -5
```

Expected: green summary.

- [ ] **Step 5.7: Open PR**

```bash
cat > /tmp/run-stream-body.md <<'EOF'
## Summary

`mship run` now streams every service's stdout and stderr to our stdout in real time, prefixed with the repo name. Both foreground and background services were previously silent (foreground: captured-and-never-printed; background: unread PIPEs could fill up and block the child). Both paths now flow through a shared `StreamPrinter` + per-service drain threads.

Prefix format matches docker-compose: `<repo-padded>  | <line>`, per-repo color when stdout is a TTY, bare text when piped.

## Scope

- Only `canonical_task == "run"` streams. `test`, `setup`, and other tasks keep the capture-and-return path (their output is structured and consumed programmatically).
- Stdout and stderr share the same prefix (matches docker-compose; keeps the split-on-pipe rule clean).
- No new CLI flags. TTY auto-detection for color.

## New files

- `src/mship/util/stream_printer.py` — `StreamPrinter` class + `drain_to_printer` helper. Single responsibility, no Rich dependency (raw ANSI).

## Modified files

- `src/mship/core/executor.py` — `execute()` constructs the printer when `canonical_task == "run"`; `_execute_one` routes both foreground and background `run` through `Popen` + drain threads.
- `src/mship/util/shell.py` — `run_streaming` gains an optional `env=` kwarg (so foreground `run` can pass `upstream_env` through).

## Test plan

- [x] `tests/util/test_stream_printer.py`: 10 unit tests (width, color, auto-TTY-detect, palette determinism, thread-safety under 1000 interleaved writes).
- [x] `tests/util/test_shell_streaming.py`: 6 unit tests for `drain_to_printer` with a fake Popen (stdout, stderr, None streams, multi-repo, return shape, daemon flag).
- [x] `tests/core/test_executor_run_streaming.py`: 5 integration tests with real subprocesses (single foreground, non-zero exit, parallel tier, background, non-run isolation).
- [x] Existing `tests/core/test_executor.py` still green — unchanged tasks (setup, test, etc.) unaffected.
- [x] Manual smoke in a scratch workspace: two services, interleaved prefixed output visible in real time; ANSI present on TTY, absent on `| tee`; non-zero exit propagates with inline error visible.

Fixes the reported adoption blocker: `mship run` no longer looks broken when services fail to start.
EOF

mship finish --body-file /tmp/run-stream-body.md
```

Expected: PR URL returned.

---

## Done when

- [x] `StreamPrinter` renders `<repo-padded>  | <content>` lines thread-safely; ANSI applied when `use_color=True` (auto when TTY).
- [x] `drain_to_printer` spawns daemon threads that read Popen PIPEs and feed the printer; works with None streams.
- [x] Executor foreground `run`: `Popen` + drain + wait + join — output appears live, returncode propagates to `ShellResult`.
- [x] Executor background `run`: `Popen` + drain (threads continue after `_execute_one` returns).
- [x] Non-`run` canonical tasks (setup, test, ...) unchanged — still use `run_task` capture path.
- [x] `run_streaming` accepts `env=` kwarg so foreground `run` can pass `upstream_env`.
- [x] All existing tests pass; 21 new tests pass (10 + 6 + 5).
- [x] Manual smoke confirms interleaved live output, color on TTY, no color when piped, non-zero exit visible.

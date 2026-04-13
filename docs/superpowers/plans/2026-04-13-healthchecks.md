# Healthchecks & Readiness Gates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-repo `healthcheck` config with TCP / HTTP / sleep / task probes, integrate into the executor so `mship run` waits for readiness before starting dependent services.

**Architecture:** New `HealthcheckRunner` (stdlib only — `socket`, `urllib.request`, `time`). Executor runs healthchecks after each tier's launches complete. Failed healthcheck = failed repo = fail-fast + kill backgrounds.

**Tech Stack:** Python 3.14, Pydantic v2, `socket`, `urllib.request`, `time` — no new deps.

---

## File Map

- `src/mship/core/config.py` — `Healthcheck` model, `RepoConfig.healthcheck` field
- `src/mship/core/healthcheck.py` — (new) `HealthcheckRunner` with four probe types + duration parser
- `src/mship/core/executor.py` — `RepoResult.healthcheck` field; run healthcheck per tier
- `src/mship/container.py` — `healthcheck_runner` provider; wire into executor
- `src/mship/cli/exec.py` — append healthcheck status to startup summary
- `README.md` — new "Healthchecks" subsection
- Tests for each layer

---

### Task 1: Healthcheck Model in Config

**Files:**
- Modify: `src/mship/core/config.py`
- Modify: `tests/core/test_config.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_config.py`:

```python
from mship.core.config import Healthcheck


def test_healthcheck_tcp_probe():
    hc = Healthcheck(tcp="127.0.0.1:8001")
    assert hc.tcp == "127.0.0.1:8001"
    assert hc.http is None
    assert hc.timeout == "30s"
    assert hc.retry_interval == "500ms"


def test_healthcheck_requires_exactly_one_probe():
    import pytest
    with pytest.raises(ValueError, match="exactly one"):
        Healthcheck()
    with pytest.raises(ValueError, match="exactly one"):
        Healthcheck(tcp="127.0.0.1:8001", http="http://localhost/health")


def test_healthcheck_custom_timeout_and_interval():
    hc = Healthcheck(tcp="127.0.0.1:8001", timeout="60s", retry_interval="1s")
    assert hc.timeout == "60s"
    assert hc.retry_interval == "1s"


def test_repo_healthcheck_default_none(workspace):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.repos["shared"].healthcheck is None


def test_repo_healthcheck_loaded(workspace):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    healthcheck:
      tcp: "127.0.0.1:8001"
      timeout: 45s
"""
    )
    config = ConfigLoader.load(cfg)
    hc = config.repos["shared"].healthcheck
    assert hc is not None
    assert hc.tcp == "127.0.0.1:8001"
    assert hc.timeout == "45s"


def test_repo_healthcheck_http(workspace):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    healthcheck:
      http: "http://localhost:8000/health"
"""
    )
    config = ConfigLoader.load(cfg)
    assert config.repos["shared"].healthcheck.http == "http://localhost:8000/health"


def test_repo_healthcheck_task(workspace):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    healthcheck:
      task: wait-for-db
      timeout: 60s
"""
    )
    config = ConfigLoader.load(cfg)
    hc = config.repos["shared"].healthcheck
    assert hc.task == "wait-for-db"
    assert hc.timeout == "60s"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_config.py -v -k "healthcheck"`
Expected: FAIL — `Healthcheck` model doesn't exist.

- [ ] **Step 3: Add `Healthcheck` model to `src/mship/core/config.py`**

Add this class before `RepoConfig`:

```python
class Healthcheck(BaseModel):
    tcp: str | None = None
    http: str | None = None
    sleep: str | None = None
    task: str | None = None
    timeout: str = "30s"
    retry_interval: str = "500ms"

    @model_validator(mode="after")
    def exactly_one_probe(self) -> "Healthcheck":
        probes = [self.tcp, self.http, self.sleep, self.task]
        set_count = sum(1 for p in probes if p is not None)
        if set_count != 1:
            raise ValueError(
                "healthcheck must specify exactly one of: tcp, http, sleep, task"
            )
        return self
```

Add field to `RepoConfig`:
```python
    healthcheck: Healthcheck | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_config.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/config.py tests/core/test_config.py
git commit -m "feat: add Healthcheck model and RepoConfig.healthcheck field"
```

---

### Task 2: HealthcheckRunner Core Module

**Files:**
- Create: `src/mship/core/healthcheck.py`
- Create: `tests/core/test_healthcheck.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_healthcheck.py`:

```python
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.config import Healthcheck
from mship.core.healthcheck import (
    HealthcheckRunner,
    HealthcheckResult,
    _parse_duration,
)
from mship.util.shell import ShellRunner, ShellResult


def test_parse_duration_seconds():
    assert _parse_duration("30s") == 30.0


def test_parse_duration_ms():
    assert _parse_duration("500ms") == 0.5


def test_parse_duration_minutes():
    assert _parse_duration("2m") == 120.0


def test_parse_duration_invalid():
    with pytest.raises(ValueError):
        _parse_duration("nope")


def test_sleep_probe_always_ready():
    runner = HealthcheckRunner(MagicMock(spec=ShellRunner))
    hc = Healthcheck(sleep="50ms")
    result = runner.wait(hc, Path("."))
    assert result.ready
    assert "slept" in result.message
    assert result.duration_s >= 0.04  # some slack


def test_tcp_probe_ready_when_port_open():
    # Start a local TCP server
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen(1)

    try:
        runner = HealthcheckRunner(MagicMock(spec=ShellRunner))
        hc = Healthcheck(tcp=f"127.0.0.1:{port}", timeout="2s", retry_interval="100ms")
        result = runner.wait(hc, Path("."))
        assert result.ready
        assert "ready after" in result.message
        assert "tcp" in result.message
    finally:
        server.close()


def test_tcp_probe_timeout_when_port_closed():
    runner = HealthcheckRunner(MagicMock(spec=ShellRunner))
    # Use a port nothing is listening on
    hc = Healthcheck(tcp="127.0.0.1:1", timeout="300ms", retry_interval="100ms")
    result = runner.wait(hc, Path("."))
    assert not result.ready
    assert "timeout" in result.message


def test_http_probe_ready_when_server_responds():
    # Start a tiny HTTP server returning 200
    class OKHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args, **kwargs):
            pass  # silence test noise

    server = HTTPServer(("127.0.0.1", 0), OKHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        runner = HealthcheckRunner(MagicMock(spec=ShellRunner))
        hc = Healthcheck(
            http=f"http://127.0.0.1:{port}/", timeout="2s", retry_interval="100ms"
        )
        result = runner.wait(hc, Path("."))
        assert result.ready
        assert "http" in result.message
    finally:
        server.shutdown()


def test_http_probe_timeout_when_no_server():
    runner = HealthcheckRunner(MagicMock(spec=ShellRunner))
    hc = Healthcheck(
        http="http://127.0.0.1:1/nowhere",
        timeout="300ms",
        retry_interval="100ms",
    )
    result = runner.wait(hc, Path("."))
    assert not result.ready
    assert "timeout" in result.message


def test_task_probe_ready_when_exit_0():
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    runner = HealthcheckRunner(mock_shell)
    hc = Healthcheck(task="wait-ready", timeout="2s", retry_interval="100ms")
    result = runner.wait(hc, Path("."))
    assert result.ready
    assert "task" in result.message


def test_task_probe_timeout_when_exit_nonzero():
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=1, stdout="", stderr="not ready")
    runner = HealthcheckRunner(mock_shell)
    hc = Healthcheck(task="check", timeout="300ms", retry_interval="100ms")
    result = runner.wait(hc, Path("."))
    assert not result.ready
    assert "not ready" in result.message or "timeout" in result.message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_healthcheck.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Create `src/mship/core/healthcheck.py`**

```python
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

from pydantic import BaseModel

from mship.core.config import Healthcheck
from mship.util.shell import ShellRunner


class HealthcheckResult(BaseModel):
    ready: bool
    message: str
    duration_s: float


def _parse_duration(s: str) -> float:
    """Parse '30s', '500ms', '2m' into seconds."""
    if s.endswith("ms"):
        return float(s[:-2]) / 1000
    if s.endswith("s"):
        return float(s[:-1])
    if s.endswith("m"):
        return float(s[:-1]) * 60
    raise ValueError(f"Invalid duration: {s!r}")


class HealthcheckRunner:
    def __init__(self, shell: ShellRunner) -> None:
        self._shell = shell

    def wait(
        self,
        healthcheck: Healthcheck,
        repo_path: Path,
        env_runner: str | None = None,
    ) -> HealthcheckResult:
        timeout_s = _parse_duration(healthcheck.timeout)
        interval_s = _parse_duration(healthcheck.retry_interval)
        start = time.monotonic()
        deadline = start + timeout_s

        # sleep probe: unconditional
        if healthcheck.sleep is not None:
            sleep_s = _parse_duration(healthcheck.sleep)
            time.sleep(sleep_s)
            return HealthcheckResult(
                ready=True,
                message=f"slept {healthcheck.sleep}",
                duration_s=sleep_s,
            )

        probe_label = self._probe_label(healthcheck)
        last_error = "no attempts"

        while True:
            elapsed = time.monotonic() - start

            if healthcheck.tcp is not None:
                ok, err = self._probe_tcp(healthcheck.tcp)
            elif healthcheck.http is not None:
                ok, err = self._probe_http(healthcheck.http)
            elif healthcheck.task is not None:
                ok, err = self._probe_task(healthcheck.task, repo_path, env_runner)
            else:
                ok, err = False, "no probe configured"

            if ok:
                return HealthcheckResult(
                    ready=True,
                    message=f"ready after {elapsed:.1f}s ({probe_label})",
                    duration_s=elapsed,
                )

            last_error = err
            if time.monotonic() + interval_s > deadline:
                break
            time.sleep(interval_s)

        elapsed = time.monotonic() - start
        return HealthcheckResult(
            ready=False,
            message=f"timeout after {elapsed:.1f}s ({probe_label}): {last_error}",
            duration_s=elapsed,
        )

    def _probe_label(self, hc: Healthcheck) -> str:
        if hc.tcp:
            return f"tcp {hc.tcp}"
        if hc.http:
            return f"http {hc.http}"
        if hc.task:
            return f"task {hc.task}"
        return "sleep"

    def _probe_tcp(self, addr: str) -> tuple[bool, str]:
        try:
            host, port_str = addr.rsplit(":", 1)
            port = int(port_str)
        except ValueError:
            return False, f"invalid tcp address: {addr}"
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True, ""
        except (OSError, socket.timeout) as e:
            return False, str(e)

    def _probe_http(self, url: str) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if 200 <= resp.status < 300:
                    return True, ""
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}"
        except (urllib.error.URLError, OSError, socket.timeout) as e:
            return False, str(e)

    def _probe_task(
        self, task_name: str, repo_path: Path, env_runner: str | None
    ) -> tuple[bool, str]:
        result = self._shell.run_task(
            task_name=task_name,
            actual_task_name=task_name,
            cwd=repo_path,
            env_runner=env_runner,
        )
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip()[:100] or f"exit {result.returncode}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_healthcheck.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/healthcheck.py tests/core/test_healthcheck.py
git commit -m "feat: add HealthcheckRunner with tcp/http/sleep/task probes"
```

---

### Task 3: Executor Integration

**Files:**
- Modify: `src/mship/core/executor.py`
- Modify: `src/mship/container.py`
- Modify: `tests/core/test_executor.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_executor.py`:

```python
def test_executor_runs_healthcheck_after_launch(workspace):
    """When a repo has healthcheck and runs successfully, healthcheck runs and attaches result."""
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    start_mode: background
    healthcheck:
      sleep: 10ms
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 12345
    mock_shell.run_streaming.return_value = popen_mock

    from mship.core.healthcheck import HealthcheckRunner
    hc_runner = HealthcheckRunner(mock_shell)

    executor = RepoExecutor(config, graph, state_mgr, mock_shell, healthcheck=hc_runner)
    result = executor.execute("run", repos=["shared"])

    assert result.success
    assert result.results[0].healthcheck is not None
    assert result.results[0].healthcheck.ready


def test_executor_healthcheck_failure_fails_repo(workspace):
    """A failed healthcheck marks the repo as failed and fail-fast triggers."""
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    start_mode: background
    healthcheck:
      tcp: "127.0.0.1:1"
      timeout: 100ms
      retry_interval: 50ms
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 12345
    mock_shell.run_streaming.return_value = popen_mock
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    from mship.core.healthcheck import HealthcheckRunner
    hc_runner = HealthcheckRunner(mock_shell)

    executor = RepoExecutor(config, graph, state_mgr, mock_shell, healthcheck=hc_runner)
    result = executor.execute("run", repos=["shared", "auth-service"])

    assert not result.success
    # shared should have failed its healthcheck
    shared_result = next(r for r in result.results if r.repo == "shared")
    assert not shared_result.success
    assert shared_result.healthcheck is not None
    assert not shared_result.healthcheck.ready
    # auth-service should NOT have been started
    auth_called = any(r.repo == "auth-service" for r in result.results)
    assert not auth_called


def test_executor_skips_healthcheck_for_test_command(workspace):
    """Healthchecks only apply to `run` canonical task, not test."""
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    healthcheck:
      tcp: "127.0.0.1:1"
      timeout: 100ms
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    from mship.core.healthcheck import HealthcheckRunner
    hc_runner = HealthcheckRunner(mock_shell)

    executor = RepoExecutor(config, graph, state_mgr, mock_shell, healthcheck=hc_runner)
    result = executor.execute("test", repos=["shared"])
    # Test command should succeed — no healthcheck
    assert result.success
    assert result.results[0].healthcheck is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_executor.py -v -k "healthcheck"`
Expected: FAIL — `RepoExecutor.__init__` doesn't accept `healthcheck`, `RepoResult` doesn't have `healthcheck` attribute.

- [ ] **Step 3: Update `RepoResult` dataclass in `src/mship/core/executor.py`**

Add the `healthcheck` field and update `success` property. Import `HealthcheckResult` at top:

```python
from mship.core.healthcheck import HealthcheckResult
```

Update the dataclass:

```python
@dataclass
class RepoResult:
    repo: str
    task_name: str
    shell_result: ShellResult
    skipped: bool = False
    background_pid: int | None = None
    healthcheck: HealthcheckResult | None = None

    @property
    def success(self) -> bool:
        if self.skipped:
            return True
        if self.shell_result.returncode != 0:
            return False
        if self.healthcheck is not None and not self.healthcheck.ready:
            return False
        return True
```

- [ ] **Step 4: Update `RepoExecutor.__init__` to accept `healthcheck`**

Find `__init__` and add the parameter:

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

- [ ] **Step 5: Add healthcheck block in `execute` per tier**

Find the `execute` method. After the tier results are collected and sorted (around where it does `tier_results.sort(key=lambda r: r.repo)`), add healthcheck execution before the existing test-results save and fail-fast check:

```python
            tier_results.sort(key=lambda r: r.repo)

            # Run healthchecks for this tier (only for `run` canonical task)
            if canonical_task == "run":
                for repo_result in tier_results:
                    repo_config = self._config.repos[repo_result.repo]
                    if repo_config.healthcheck is None:
                        continue
                    if not repo_result.success:
                        # Task launch failed — skip healthcheck
                        continue
                    cwd = self._resolve_cwd(repo_result.repo, task_slug)
                    env_runner = self.resolve_env_runner(repo_result.repo)
                    hc_result = self._healthcheck.wait(
                        repo_config.healthcheck, cwd, env_runner
                    )
                    repo_result.healthcheck = hc_result
                    if not hc_result.ready:
                        # Overwrite shell_result to surface the healthcheck failure message
                        repo_result.shell_result = ShellResult(
                            returncode=1,
                            stdout=repo_result.shell_result.stdout,
                            stderr=hc_result.message,
                        )

            result.results.extend(tier_results)
            result.background_processes.extend(tier_backgrounds)

            # ... existing test-result batch save and fail-fast ...
```

**Important:** After the fail-fast break, existing code already terminates background processes (that logic is in the CLI layer). The executor just needs to mark the repos failed so downstream tiers don't start.

Read the current `execute()` method to place this block correctly — it should go AFTER the parallel launch (tier_results fully populated) and BEFORE the test-result save and tier-success check.

- [ ] **Step 6: Update container in `src/mship/container.py`**

Add import:
```python
from mship.core.healthcheck import HealthcheckRunner
```

Add provider (after `shell`):
```python
    healthcheck_runner = providers.Singleton(
        HealthcheckRunner,
        shell=shell,
    )
```

Update `executor` provider to pass `healthcheck`:
```python
    executor = providers.Factory(
        RepoExecutor,
        config=config,
        graph=graph,
        state_manager=state_manager,
        shell=shell,
        healthcheck=healthcheck_runner,
    )
```

- [ ] **Step 7: Fix any existing executor tests that construct RepoExecutor without healthcheck**

Run: `uv run pytest tests/core/test_executor.py -v`
Expected: most existing tests will fail with `TypeError: missing 1 required positional argument: 'healthcheck'`.

For each failing test, update `RepoExecutor(...)` construction to include `healthcheck=MagicMock()` (or a real `HealthcheckRunner(mock_shell)` if the test needs real behavior).

The simplest fix: grep for `RepoExecutor(config, graph, state_mgr, mock_shell)` and change to `RepoExecutor(config, graph, state_mgr, mock_shell, healthcheck=MagicMock())`.

- [ ] **Step 8: Run all tests**

Run: `uv run pytest tests/core/test_executor.py -v`
Expected: All tests PASS (including new healthcheck tests).

Run: `uv run pytest tests/ -q`
Expected: All tests PASS. Other CLI tests that call the executor through the container should work — the container provides the real healthcheck_runner.

- [ ] **Step 9: Commit**

```bash
git add src/mship/core/executor.py src/mship/container.py tests/core/test_executor.py
git commit -m "feat: run healthchecks per tier in executor, fail-fast on timeout"
```

---

### Task 4: CLI Output — Startup Summary with Healthcheck Status

**Files:**
- Modify: `src/mship/cli/exec.py`
- Modify: `tests/cli/test_exec.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/cli/test_exec.py`:

```python
def test_mship_run_shows_healthcheck_in_summary(workspace):
    """Startup summary includes healthcheck status per background service."""
    from mship.cli import container as cli_container
    from datetime import datetime, timezone

    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    start_mode: background
    healthcheck:
      sleep: 10ms
"""
    )
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    cli_container.config_path.override(cfg)
    cli_container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="hc-summary-test",
        description="Healthcheck summary test",
        phase="dev",
        created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/hc-summary-test",
    )
    mgr.save(WorkspaceState(current_task="hc-summary-test", tasks={"hc-summary-test": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 33333
    popen_mock.wait.return_value = 0
    popen_mock.poll.return_value = 0
    mock_shell.run_streaming.return_value = popen_mock
    mock_shell.build_command.return_value = "task run"
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    # Should mention sleep probe succeeded
    assert "slept" in result.output or "ready" in result.output

    cli_container.config_path.reset_override()
    cli_container.state_dir.reset_override()
    cli_container.config.reset()
    cli_container.state_manager.reset()
    cli_container.shell.reset_override()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cli/test_exec.py -v -k "healthcheck_in_summary"`
Expected: FAIL — startup summary doesn't include healthcheck info.

- [ ] **Step 3: Update `run_cmd` startup summary in `src/mship/cli/exec.py`**

Find this block:

```python
        output.success(f"Started {len(result.background_processes)} background service(s):")
        for repo_result in result.results:
            if repo_result.background_pid is not None:
                output.print(
                    f"  [green]✓[/green] {repo_result.repo} → task {repo_result.task_name}  (pid {repo_result.background_pid})"
                )
        output.print("")
        output.print("Press Ctrl-C to stop.")
```

Replace with:

```python
        output.success(f"Started {len(result.background_processes)} background service(s):")
        for repo_result in result.results:
            if repo_result.background_pid is None and repo_result.healthcheck is None:
                continue
            pid_part = f"(pid {repo_result.background_pid})" if repo_result.background_pid else ""
            hc_part = f"  {repo_result.healthcheck.message}" if repo_result.healthcheck else ""
            icon = "[green]✓[/green]" if repo_result.success else "[red]✗[/red]"
            output.print(
                f"  {icon} {repo_result.repo} → task {repo_result.task_name}  {pid_part}{hc_part}"
            )
        output.print("")
        output.print("Press Ctrl-C to stop.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/cli/test_exec.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/exec.py tests/cli/test_exec.py
git commit -m "feat: mship run startup summary includes healthcheck status"
```

---

### Task 5: README Documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add Healthchecks subsection**

Find the "Service Start Modes (`start_mode`)" subsection in the README. Add a new subsection after it, before "Task Name Aliasing":

```markdown
### Healthchecks

For services that need time to become ready (databases, dev servers binding to ports), declare a `healthcheck`. `mship run` waits for the healthcheck to pass before starting dependent services.

```yaml
repos:
  infra:
    path: ./infra
    type: service
    start_mode: background
    healthcheck:
      tcp: "127.0.0.1:8001"          # wait for port to accept connections
      timeout: 30s                    # optional, default 30s
      retry_interval: 500ms           # optional, default 500ms

  backend:
    path: ./backend
    type: service
    start_mode: background
    depends_on: [infra]
    healthcheck:
      http: "http://localhost:8000/health"   # wait for 2xx response

  web:
    path: ./web
    type: service
    start_mode: background
    depends_on: [backend]
    healthcheck:
      sleep: 3s                        # unconditional wait

  custom:
    path: ./custom
    type: service
    start_mode: background
    healthcheck:
      task: wait-for-custom            # invokes `task wait-for-custom`, 0 exit = ready
```

**Probe types:**
- `tcp: host:port` — succeeds when a TCP connection is accepted
- `http: url` — succeeds on a 2xx response
- `sleep: duration` — waits unconditionally (for things you can't probe)
- `task: task-name` — runs a Taskfile task; 0 exit = ready

Exactly one probe per healthcheck. If the probe doesn't succeed within `timeout`, the service is treated as failed, background processes are terminated, and `mship run` exits non-zero.

Healthchecks apply to `mship run` only — `mship test` ignores them.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add Healthchecks section to README"
```

---

### Task 6: Integration Test

**Files:**
- Modify: `tests/test_monorepo_integration.py`

- [ ] **Step 1: Add integration test**

Add to `tests/test_monorepo_integration.py`:

```python
def test_monorepo_healthcheck_sleep_succeeds(monorepo_workspace):
    """Integration: a repo with a sleep healthcheck reports ready in the summary."""
    tmp_path, mock_shell = monorepo_workspace

    # Rewrite config to add healthchecks
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: tailrd
repos:
  tailrd:
    path: ./tailrd
    type: service
    tasks:
      run: dev
    start_mode: background
    healthcheck:
      sleep: 10ms
  web:
    path: web
    type: service
    git_root: tailrd
    tasks:
      run: dev
    start_mode: background
    depends_on: [tailrd]
    healthcheck:
      sleep: 10ms
"""
    )

    runner.invoke(app, ["spawn", "hc integration", "--skip-setup"])

    popen_mocks = []
    def make_popen(*args, **kwargs):
        p = MagicMock()
        p.pid = 90000 + len(popen_mocks)
        p.wait.return_value = 0
        p.poll.return_value = 0
        popen_mocks.append(p)
        return p
    mock_shell.run_streaming.side_effect = make_popen

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    # Both services should have healthcheck success in output
    assert "slept" in result.output or "ready" in result.output
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_monorepo_integration.py -v`
Expected: All tests PASS.

- [ ] **Step 3: Full suite + CLI help**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

Run: `uv run mship --help`
Expected: Same commands as before (no CLI changes).

- [ ] **Step 4: Commit and push**

```bash
git add tests/test_monorepo_integration.py
git commit -m "test: add integration test for healthchecks"
git push
```

---

## Self-Review

**Spec coverage:**
- Healthcheck model + validator: Task 1
- TCP / HTTP / sleep / task probes: Task 2
- Duration parser: Task 2
- Per-tier healthcheck execution: Task 3
- Failure = failed repo = fail-fast: Task 3
- `mship test` ignores healthchecks: Task 3 (only runs when `canonical_task == "run"`)
- DI container wiring: Task 3
- Startup summary with healthcheck status: Task 4
- README docs: Task 5
- Integration: Task 6

**Placeholder scan:** No TBDs. All code complete.

**Type consistency:**
- `Healthcheck` Pydantic model across config/healthcheck/executor
- `HealthcheckResult` Pydantic model returned from `HealthcheckRunner.wait`
- `RepoResult.healthcheck: HealthcheckResult | None` matches across executor and CLI
- `RepoExecutor` constructor signature consistent: `(config, graph, state_manager, shell, healthcheck)`

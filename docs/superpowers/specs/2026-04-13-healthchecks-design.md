# Healthchecks & Readiness Gates Design Spec

## Overview

Add readiness gates between services so `mship run` waits for upstream services (e.g. DynamoDB on port 8001, a backend HTTP health endpoint) before starting downstream ones. Solves three related real-world pain points: services start against dead infra, no way to wait for an API to accept connections, `docker compose up -d` exits before containers are ready.

Mothership provides four built-in probe types (TCP, HTTP, sleep, Taskfile task) so the common cases need zero Taskfile boilerplate.

## 1. Config Schema

New optional `healthcheck` field on `RepoConfig`:

```yaml
repos:
  infra:
    start_mode: background
    healthcheck:
      tcp: "127.0.0.1:8001"       # wait for port
      timeout: 30s
      retry_interval: 500ms

  backend:
    start_mode: background
    depends_on: [infra]
    healthcheck:
      http: "http://localhost:8000/health"

  web:
    start_mode: background
    depends_on: [backend]
    healthcheck:
      sleep: 3s                   # unconditional wait

  exotic:
    start_mode: background
    healthcheck:
      task: wait-for-custom       # invokes `task wait-for-custom`
      timeout: 60s
```

### Pydantic model

In `src/mship/core/config.py`:

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

Added to `RepoConfig`:
```python
healthcheck: Healthcheck | None = None
```

### Defaults

- `timeout: "30s"` if omitted
- `retry_interval: "500ms"` if omitted
- No healthcheck at all = current behavior (start and proceed immediately)

### Applies to

- Both `start_mode: foreground` and `start_mode: background`
- Only runs for `canonical_task == "run"` (not test/lint/setup)
- `mship test` ignores healthchecks entirely

## 2. HealthcheckRunner

New file `src/mship/core/healthcheck.py`:

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
    message: str       # "ready after 2.3s (tcp 127.0.0.1:8001)" or "timeout after 30s: ..."
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

        # sleep probe: unconditional, always succeeds
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
        if hc.tcp: return f"tcp {hc.tcp}"
        if hc.http: return f"http {hc.http}"
        if hc.task: return f"task {hc.task}"
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

**No new dependencies** — `socket`, `urllib.request`, `time` are stdlib.

## 3. Executor Integration

### Per-tier flow (updated)

```
For each tier:
  In parallel: launch task run for each repo, collect (RepoResult, background_pid)
  Wait for tier execution to complete (foregrounds exit, backgrounds launched)

  For each repo in tier (sequentially):
    If repo has healthcheck:
      Run HealthcheckRunner.wait(...)
      Store result on RepoResult.healthcheck
      If not ready: mark RepoResult as failed (set shell_result.returncode=1)

  If any repo failed (including healthcheck): kill all background processes, stop
  Otherwise: proceed to next tier
```

Healthchecks run **after** the tier's launches finish (so all repos in a tier start in parallel, then each is probed in order). This matches the existing parallel-within-tier model.

### Model changes

`src/mship/core/executor.py`:

```python
@dataclass
class RepoResult:
    repo: str
    task_name: str
    shell_result: ShellResult
    skipped: bool = False
    background_pid: int | None = None
    healthcheck: "HealthcheckResult | None" = None  # new

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

`ExecutionResult` — no change (existing `background_processes` list + per-repo `healthcheck` on each `RepoResult` is sufficient).

### Executor integration point

After the parallel-launch block in `execute()`, before the "fail-fast between tiers" check:

```python
# Run healthchecks for this tier (after all launches complete)
if canonical_task == "run":
    for repo_result in tier_results:
        repo_config = self._config.repos[repo_result.repo]
        if repo_config.healthcheck is None:
            continue
        if not repo_result.success:
            # Task already failed, skip healthcheck
            continue
        cwd = self._resolve_cwd(repo_result.repo, task_slug)
        env_runner = self.resolve_env_runner(repo_result.repo)
        hc_result = self._healthcheck.wait(
            repo_config.healthcheck, cwd, env_runner,
        )
        repo_result.healthcheck = hc_result
        if not hc_result.ready:
            # Mark the repo as failed so fail-fast triggers and backgrounds get killed
            repo_result.shell_result = ShellResult(
                returncode=1,
                stdout=repo_result.shell_result.stdout,
                stderr=hc_result.message,
            )
```

### Constructor / DI

`RepoExecutor.__init__` gains a `healthcheck: HealthcheckRunner` parameter. Container update:

```python
healthcheck_runner = providers.Singleton(
    HealthcheckRunner,
    shell=shell,
)

executor = providers.Factory(
    RepoExecutor,
    config=config,
    graph=graph,
    state_manager=state_manager,
    shell=shell,
    healthcheck=healthcheck_runner,
)
```

## 4. CLI Output

In `src/mship/cli/exec.py::run_cmd`, the startup summary already iterates `result.results`. Extend the per-line output to append healthcheck status when present:

**Current line (no healthcheck):**
```
  ✓ infra → task run  (pid 12345)
```

**With successful healthcheck:**
```
  ✓ infra → task run  (pid 12345)  ready after 1.8s (tcp 127.0.0.1:8001)
```

**With failed healthcheck (shown instead of success):**
```
  ✗ backend → task run  (pid 12346)  healthcheck failed: timeout after 30s (http http://localhost:8000/health): Connection refused
```

Implementation sketch (inside the existing summary loop):

```python
for repo_result in result.results:
    if repo_result.background_pid is not None or ...:  # pseudocode
        pid_part = f"(pid {repo_result.background_pid})" if repo_result.background_pid else ""
        hc_part = ""
        if repo_result.healthcheck is not None:
            hc_part = f"  {repo_result.healthcheck.message}"
        status_icon = "[green]✓[/green]" if repo_result.success else "[red]✗[/red]"
        output.print(
            f"  {status_icon} {repo_result.repo} → task {repo_result.task_name}  {pid_part}{hc_part}"
        )
```

On full failure (tier failure, backgrounds killed), the existing error-path output handles the rest; we just need to surface the healthcheck message via `repo_result.shell_result.stderr` which now contains it.

## Files Changed / Created

| File | Change |
|------|--------|
| `src/mship/core/config.py` | Add `Healthcheck` model and `RepoConfig.healthcheck` field |
| `src/mship/core/healthcheck.py` | Create: `HealthcheckRunner`, probes, duration parser |
| `src/mship/core/executor.py` | Add `healthcheck` param; run healthcheck per tier; update `RepoResult` |
| `src/mship/container.py` | Add `healthcheck_runner` provider; wire into executor |
| `src/mship/cli/exec.py` | Append healthcheck status to startup summary |
| `README.md` | Add "Healthchecks" subsection with the four probe types |
| `tests/core/test_config.py` | Test healthcheck model + exactly-one-probe validator |
| `tests/core/test_healthcheck.py` | Create: TCP/HTTP/sleep/task probes, duration parser, timeout |
| `tests/core/test_executor.py` | Test healthcheck runs per tier, failure kills backgrounds |
| `tests/cli/test_exec.py` | Test run output includes healthcheck status |
| `tests/test_monorepo_integration.py` | Add an integration test for healthchecks |

## Non-Goals

- No per-service timeout override beyond `timeout:` (global pattern matching is out of scope)
- No retry policies beyond simple retry_interval (exponential backoff is out of scope)
- No healthcheck caching across runs (every `mship run` re-probes)
- `mship test` and other commands explicitly ignore healthchecks

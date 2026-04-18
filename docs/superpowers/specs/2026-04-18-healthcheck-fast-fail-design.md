# Healthcheck fast-fail on background-process crash — Design

## Context

`mship run` launches background services and then waits for each service's healthcheck to pass before advancing to the next dependency tier. When a background service's `run` task crashes immediately (e.g., missing binary, syntax error, permission denied), the Popen exits within milliseconds but mship has no awareness of that exit until the healthcheck eventually times out — typically 60 seconds, sometimes more.

Observed in a real workspace (`tailrd`):

```
infra   | task: [infra:start] docker run -d -p 8001:8000 ... dynamodb-local
tailrd  | task: [infra:ensure] aws dynamodb describe-table ...
tailrd  | "aws": executable file not found in $PATH
tailrd  | task: Failed to run task "dev": ... exit status 127
infra   | dynamodb-local
infra   | task: [infra:start] docker run -d ... minio
infra   | minio
ERROR: tailrd: failed to start
```

`tailrd` died at exit 127 within a second of starting. mship then spent ~60s waiting on a http :8000/health that was never going to respond, before printing the final error. The CLI exit message also doesn't reflect the real cause — it surfaces the healthcheck timeout, not the 127 returncode or the missing-binary stderr.

Root cause: the `start_mode=background` branch of `RepoExecutor._execute_one` returns `ShellResult(returncode=0, ...)` unconditionally (the process hasn't been waited on), and the tier-level `HealthcheckRunner.wait()` loop has no visibility into the Popen. So a crashed background process is indistinguishable from one that is slow to open its port.

## Goal

When a background service's `run` Popen exits with a non-zero returncode, `mship run` should detect the crash within one healthcheck retry interval and surface the real error, instead of waiting the full healthcheck timeout.

Detection mechanism: the executor threads the Popen into `HealthcheckRunner.wait()`, which polls the process on every retry and bails out on non-zero exit. Exit-0 is intentionally ignored — many legitimate `run` tasks (e.g., `docker run -d ...`) detach and exit cleanly while the actual service runs in the background.

## Success criterion

Given a workspace with one background service whose `run` task is `sh -c 'exit 127'` and a healthcheck (any kind, with a 60s timeout), `mship run` exits with code 1 within ~2 seconds (one retry interval of overhead). The `result.results[0].healthcheck.message` reads:

```
background process exited with code 127 before http http://... passed
```

For the `tailrd` example above, `mship run` reports the tailrd failure within a couple of seconds — before `infra`'s trailing docker output even finishes — and the healthcheck message explicitly names the exit code.

## Anti-goals

- **No change to foreground `run`.** Foreground tasks already call `popen.wait()` and propagate the real returncode. They're not affected by this bug.
- **No detection for background services without a healthcheck.** If a repo is `start_mode: background` with no `healthcheck` block, mship never enters the `wait()` loop and has no place to poll. Leaving that case unchanged is an explicit scope boundary; a separate fix can add post-spawn polling for always-background services if it proves needed.
- **No post-hoc detection.** Adding a watchdog thread per Popen, or a per-tier scan after healthcheck returns, would not shorten the 60-second wait. The fix must live inside the healthcheck retry loop.
- **No treatment of exit-0 as a crash.** `docker run -d ...`, `systemctl start foo`, and similar service-handoff patterns exit cleanly when the service is up. Healthcheck must keep probing until it confirms readiness or times out.
- **No change to healthcheck output format for the timeout path.** When the process is still alive at deadline, the existing `"timeout after Xs (label): last_error"` message is unchanged.
- **No new CLI flags or config keys.** Behavior is automatic.

## Architecture

### `HealthcheckRunner.wait()` — new `proc` parameter

`src/mship/core/healthcheck.py`:

```python
def wait(
    self,
    healthcheck: Healthcheck,
    repo_path: Path,
    env_runner: str | None = None,
    proc: "subprocess.Popen | None" = None,
) -> HealthcheckResult:
    ...
    while True:
        elapsed = time.monotonic() - start

        # Bail early if the background process has already crashed.
        # Exit 0 is ignored — many services exit cleanly after detaching
        # (e.g., `docker run -d`) and the probe is still the right signal.
        if proc is not None:
            rc = proc.poll()
            if rc is not None and rc != 0:
                return HealthcheckResult(
                    ready=False,
                    message=f"background process exited with code {rc} before {probe_label} passed",
                    duration_s=elapsed,
                )

        # ... existing probe logic (tcp / http / task) unchanged ...
```

The poll check runs before each probe attempt. Existing callers that omit `proc` keep their current behavior (all existing tests stay green).

### Executor — map repo to Popen, pass into `wait()`

`src/mship/core/executor.py::execute()`:

The tier-execution block already stores background Popens in `tier_backgrounds`. Add a per-repo map so the subsequent healthcheck loop can look up the right Popen:

```python
# Inside the tier loop
tier_results: list[RepoResult] = []
tier_backgrounds: list = []
repo_to_proc: dict[str, "subprocess.Popen"] = {}

# ... after each _execute_one returns (repo_result, bg):
tier_results.append(repo_result)
if bg is not None:
    tier_backgrounds.append(bg)
    repo_to_proc[repo_result.repo] = bg
```

Update the healthcheck call to pass the matched Popen:

```python
hc_result = self._healthcheck.wait(
    repo_config.healthcheck,
    cwd,
    env_runner,
    proc=repo_to_proc.get(repo_result.repo),
)
```

For repos that aren't background (foreground `run` finishes synchronously), `repo_to_proc.get(...)` returns `None` and the poll check is skipped — current behavior preserved.

### No change to the `ShellResult` hardcode

The background branch of `_execute_one` keeps `ShellResult(returncode=0, stdout="", stderr="")` as today. The healthcheck failure path already overwrites `repo_result.shell_result` with `returncode=1` and the healthcheck message on failure (existing logic in `execute()` around lines 261-267). After this change, that message will carry the real returncode details in human-readable form, which is what users actually care about.

## Data flow

Per `mship run` invocation with a background service that crashes:

1. Tier N spawns service A's Popen via `ShellRunner.run_streaming`.
2. `_execute_one` attaches drain threads and returns `(RepoResult(success=True, background_pid=...), popen)`.
3. Executor's tier loop stores `repo_to_proc["A"] = popen` and moves on.
4. After the tier finishes spawning everyone, the healthcheck loop fires for each background repo that has a `healthcheck` configured.
5. `HealthcheckRunner.wait(..., proc=popen)` enters its retry loop:
   - Iteration 1: `proc.poll()` returns `None` (still starting) → probe runs, maybe fails, sleep 1s.
   - Iteration 2: `proc.poll()` returns `127` → wait() returns `HealthcheckResult(ready=False, message="background process exited with code 127 before http http://127.0.0.1:8000/health passed")` immediately.
6. Executor's existing hc-failure handler overwrites `repo_result.shell_result` with `returncode=1` and `stderr=<that message>`.
7. Tier success check fails → subsequent tiers are skipped (existing fail-fast logic at `executor.py:283-286`).
8. CLI prints `<repo>: failed to start`, kills any still-running background process groups, exits 1.

Total time from Popen exit to CLI exit: ≤ one retry interval (typically 500ms–1s) plus the small fixed cost of the next probe attempt. Down from up to 60s.

## Error handling

- **`proc=None` passed in:** skip the poll entirely → existing code path, no change.
- **`proc.poll()` returns a non-zero int:** return `HealthcheckResult(ready=False, ...)` with a fast-fail message — the new path.
- **`proc.poll()` returns 0:** ignore and continue probing. The process finished its detach step cleanly; whether the service actually came up is what the probe is for.
- **`proc.poll()` raises (unexpected):** let it propagate. A valid Popen's `.poll()` doesn't raise; if it does, something's structurally wrong and masking it would be worse than surfacing it.
- **Race: `.poll()` returns `None`, process exits between poll and probe return:** the probe will fail (connection refused, etc.); next retry catches the exit. Latency of one interval — acceptable.
- **`repo_to_proc.get(...)` for a repo with no background Popen:** returns `None` → poll is skipped → existing behavior. This applies to foreground runs and to the (currently-nonexistent but future) case of a background repo whose Popen spawn failed.

## Testing

### Unit — `tests/core/test_healthcheck.py` (extend existing file)

**1. Default param preserves existing behavior.** `wait(hc, path)` with no `proc` — existing assertions unchanged.

**2. Live process, probe passes.** Mock `proc.poll` returning `None` throughout; mock the probe to return `ok=True` on iteration 2. Assert `ready=True` and `proc.poll` was called twice.

**3. Immediate crash.** `proc.poll` returns `127` on the first iteration. Assert:
   - `ready=False`
   - `message` contains `"exited with code 127"`
   - `message` contains the probe label (e.g. `"tcp 127.0.0.1:8001"`)
   - Probe mock was not called even once (we bail before probing)
   - `duration_s` is near zero

**4. Exit-0 does not trigger fast-fail.** `proc.poll` returns `None` first iteration, `0` thereafter; probe stays failing. Assert `ready=False` with the timeout message (not the "exited with code" message); duration equals the configured timeout.

**5. Delayed crash.** `proc.poll` returns `None` for 2 iterations, then `2`. Assert `ready=False`, `message` contains `"exited with code 2"`, total elapsed well under timeout.

**6. Crash message names the correct probe.** With `healthcheck.http` set, the message contains `"http <url>"`, not `"tcp …"` or `"task …"`. (One sanity-check per probe type isn't needed; the existing `_probe_label` test coverage handles that.)

### Integration — `tests/core/test_executor.py` (extend)

**Real subprocess fast-fail:**
- Config: one background repo with a healthcheck (`tcp: 127.0.0.1:<unused-port>`, `timeout: 60s`, `retry_interval: 500ms`) and `tasks.run` set to `sh -c 'exit 127'`.
- Call `executor.execute("run", repos=[...])`.
- Assert the call returns in under 2 seconds (not 60+).
- Assert `result.success is False`.
- Assert `result.results[0].shell_result.returncode == 1`.
- Assert `result.results[0].healthcheck.ready is False`.
- Assert `result.results[0].healthcheck.message` contains `"exited with code 127"`.

**Live process still passes healthcheck:**
- Config: background repo with `tasks.run` = a shell command that opens a listening socket and sleeps (e.g., Python one-liner), plus a matching tcp healthcheck.
- Assert `result.success is True`, healthcheck `ready=True`.
- (This is a regression guard that exit-0 ignorance hasn't regressed the normal path — may already exist; extend only if not.)

### Regression

- All existing `test_healthcheck.py` tests must pass unchanged (new `proc` parameter defaults to `None`).
- All existing `test_executor.py` tests must pass unchanged.
- Full `pytest tests/` must stay green.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | Poll inside the healthcheck loop, not in a separate watchdog thread | The healthcheck loop is already polling on a fixed interval. Reusing that cadence keeps the fix to one method signature change + two lines of logic and avoids any thread-lifecycle concerns. |
| 2 | Exit-0 ignored, exit-nonzero fails fast | `docker run -d`, `systemctl start`, and similar handoff patterns return 0 cleanly while the real service runs elsewhere. Treating 0 as "done" would break the normal background-detach flow that many `run` tasks use. |
| 3 | Scope to background + healthcheck | These are the repos that enter `HealthcheckRunner.wait()`. A background repo with no healthcheck already advances tiers without waiting, so there's no loop to hook into. If users report crashes in that configuration, a separate post-spawn poll can be added later. |
| 4 | No change to the hardcoded `shell_result.returncode=0` in the executor's background branch | The healthcheck failure handler already overwrites `shell_result` on `hc_result.ready == False`, carrying the fast-fail message into the user-visible error. Changing the hardcode independently would require polling before `_execute_one` returns, which races with genuinely-slow-starting services. |
| 5 | `proc` parameter is keyword-only (via the existing trailing position of `env_runner`) | Backwards-compatible with every existing caller; explicit at the call site; mirrors the existing optional-parameter style. |
| 6 | Message format: `"background process exited with code N before <probe_label> passed"` | Matches the existing timeout message shape (`"timeout after Xs (probe_label): …"`) so users reading logs see consistent phrasing across failure modes. Names the exit code and the probe explicitly so the cause is unambiguous. |

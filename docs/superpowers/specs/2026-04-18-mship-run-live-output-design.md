# `mship run` — live prefixed stdout/stderr — Design

## Context

`mship run` starts services across repos but the user never sees their output. Two silent bugs in the current implementation:

1. **Background services** (`start_mode: background`): `ShellRunner.run_streaming` spawns `subprocess.Popen(stdout=PIPE, stderr=PIPE)` but nothing drains the PIPEs. Service output is invisible to the user AND the PIPE buffer can fill up, at which point the child blocks on its next write.
2. **Foreground services** (default `start_mode`): `ShellRunner.run_task` calls `subprocess.run(capture_output=True)`. Output is captured into a `ShellResult` that the CLI never prints. Even on failure, the CLI prints only `"<repo>: failed to start"` — not the actual stderr.

Either way, the user is left guessing why a service didn't come up. This is the single largest adoption blocker for `mship run` in multi-service workspaces.

## Goal

Make `mship run` stream every subprocess's stdout and stderr to our stdout in real time, prefixed with the repo name so lines from parallel services stay disambiguated. Match the docker-compose convention: `<repo-padded>  | <line>`, per-repo color when attached to a TTY, no color when piped. Apply to both `foreground` and `background` services.

## Success criterion

Given a two-repo workspace where each repo's `run` task prints to stdout and stderr, `mship run` produces output like:

```
api     | Server listening on :8080
api     | [GET /health] 200 ok
worker  | Starting queue consumer
worker  | ERROR: connection refused
api     | [GET /items] 200 ok
```

- Lines from both services interleave as they are produced (no multi-second batching).
- Prefix width is consistent — longest repo name sets the column.
- Per-repo color when `stdout.isatty()`; bare text when piped or redirected.
- `[GET /items]` and similar bracketed content in the service's own output is NOT confused with the prefix (prefix is always at line start, ends with `|`, content starts after).
- Ctrl-C terminates all services and exits cleanly.
- A service that exits non-zero: its final error output is already visible inline; the CLI's "failed to start" summary stays as today (no duplication).

## Anti-goals

- **No file persistence.** Not writing to `.mothership/logs/<repo>.log`. `mship logs` today delegates to the user's own Taskfile `logs` task — that contract stays.
- **No change to `test`, `setup`, or any other canonical task.** Only `canonical_task == "run"` switches to streaming. Other tasks still use `run_task` (capture-and-return) because their output is structured and consumed programmatically.
- **No distinct stderr prefix.** Stdout and stderr share the same prefix. Distinguishing them (e.g., `api ERR|`) adds visual noise and breaks the clean `<repo>|` regex split. Content-level markers (log levels, `Error:`) are the service's job.
- **No JSON output mode.** Streaming is for humans and agents reading the terminal. `mship context`/`dispatch` cover the structured output need.
- **No per-line timestamps.** The service can include its own. Adding ours adds column width and clutters short lines.
- **No rate-limit / throttle.** If a service floods stdout, we relay it verbatim. Users can pipe to `| head` or `| grep` as normal.
- **No change to `mship run` flags.** No new `--prefix`, `--no-color`, `--log-file`. TTY auto-detection covers color. YAGNI.

## Why prefix format `<repo>  | <line>` (option A from brainstorming)

Agents consuming `mship run` output need to split lines into `(repo, content)` reliably. Three candidates considered:

| Format | Parse risk for content containing… |
|---|---|
| `<repo>  \| <line>` | Pipe `\|` is rare in program output — split on first `\|` is near-unambiguous. |
| `[<repo>] <line>` | Brackets appear in Python tracebacks, JSON-ish formats. Greedy regex false-positives if not line-anchored. |
| `<repo>: <line>` | Colons are everywhere (timestamps, URLs, `Error:`). Splitting on first `:` breaks constantly. |

Pipe wins. It's also the docker-compose default, which means agents trained on docker-compose output parse it already.

## Architecture

### New helper — `src/mship/util/shell.py::StreamPrinter`

A thread-safe line printer scoped to a single `mship run` invocation.

```python
class StreamPrinter:
    """Prefix-and-print service output line-by-line.

    Constructed once per `mship run`; passed into each subprocess's
    drain threads. All writes go through the lock so lines never tear
    across services in the final stdout.
    """

    def __init__(self, repos: list[str], use_color: bool | None = None):
        self._width = max((len(r) for r in repos), default=0)
        self._colors = _assign_colors(repos)
        self._use_color = sys.stdout.isatty() if use_color is None else use_color
        self._lock = threading.Lock()

    def write(self, repo: str, line: str) -> None:
        prefix = f"{repo:<{self._width}}  | "
        if self._use_color and repo in self._colors:
            prefix = _colorize(prefix, self._colors[repo])
        with self._lock:
            sys.stdout.write(f"{prefix}{line.rstrip()}\n")
            sys.stdout.flush()
```

- `_assign_colors(repos)` returns `dict[str, str]` with a deterministic color per repo drawn from a fixed palette (`cyan`, `green`, `yellow`, `magenta`, `blue`, `red`) cycling in sorted-repo order. Determinism matters: re-running `mship run` with the same repo list produces the same color scheme.
- `_colorize(text, color)` wraps with ANSI escape codes (no Rich dependency in this helper — keeps it testable as a pure function with simple string output).
- `use_color=None` means "auto-detect"; explicit `True/False` is for tests.
- Output goes to `sys.stdout.write` + `flush()` rather than `print()` so we control newline handling ourselves (the service's line already ended with `\n`; we `rstrip` it and add exactly one `\n`).

### New helper — `src/mship/util/shell.py::drain_to_printer`

```python
def drain_to_printer(
    proc: subprocess.Popen,
    repo: str,
    printer: StreamPrinter,
) -> list[threading.Thread]:
    """Start daemon threads that read proc.stdout and proc.stderr line by
    line and push each line through `printer`. Returns the threads so
    callers can join() them after proc.wait() if they want to ensure all
    output is flushed before continuing.
    """
    def _drain(stream):
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                printer.write(repo, line)
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

Stdout and stderr share the same prefix — no visual differentiation. Threads are daemons so they won't block process exit if a service's PIPE is slow to close.

### Executor integration — `src/mship/core/executor.py`

**`_execute_one` when `canonical_task == "run"`:** switch from `run_task()` (capture-output) to `Popen`+drain for BOTH `start_mode` branches.

Foreground `run` (new):

```python
if canonical_task == "run" and repo_config.start_mode != "background":
    command = self._shell.build_command(f"task {actual_name}", env_runner)
    popen = self._shell.run_streaming(command, cwd=cwd)
    threads = drain_to_printer(popen, repo_name, self._printer)
    returncode = popen.wait()
    for t in threads:
        t.join(timeout=1.0)
    return (
        RepoResult(
            repo=repo_name,
            task_name=actual_name,
            shell_result=ShellResult(returncode=returncode, stdout="", stderr=""),
        ),
        None,
    )
```

Background `run` (existing code, plus one new line):

```python
if repo_config.start_mode == "background" and canonical_task == "run":
    command = self._shell.build_command(f"task {actual_name}", env_runner)
    popen = self._shell.run_streaming(command, cwd=cwd)
    drain_to_printer(popen, repo_name, self._printer)   # new
    return (
        RepoResult(
            repo=repo_name,
            task_name=actual_name,
            shell_result=ShellResult(returncode=0, stdout="", stderr=""),
            background_pid=popen.pid,
        ),
        popen,
    )
```

Drain threads for background processes are not joined — they run until the PIPE closes when the background service exits, after which `readline()` returns empty and the thread dies naturally.

**`execute()` constructs the printer once per call** when the canonical task is `run`:

```python
def execute(self, canonical_task: str, repos: list[str], ...) -> ExecutionResult:
    if canonical_task == "run":
        self._printer = StreamPrinter(repos=sorted(set(repos)))
    else:
        self._printer = None
    ...
```

`self._printer` is an instance attribute because `_execute_one` needs access. Cleared to `None` for non-run tasks so any accidental use fails loudly.

**All other canonical tasks** (`setup`, `test`, anything else) go through the existing `run_task()` capture path. Their output continues to be captured in `ShellResult.stdout`/`stderr` and consumed programmatically by the CLI (test diffs, setup warnings).

### CLI — `src/mship/cli/exec.py::run_cmd`

No changes. The existing wait-loop, signal forwarding, and kill-group logic work unchanged. The drain threads are invisible to this layer.

## Data flow

Per `mship run` invocation:

1. CLI `run_cmd` → `executor.execute("run", repos=[api, worker])`.
2. `execute()` constructs `StreamPrinter(repos=["api", "worker"])`. Width = 6 (`worker`). Colors: `api=cyan`, `worker=green` (sorted-order palette assignment).
3. Topo tiers processed in order. For each repo:
   - Foreground service: `Popen` spawned. Two drain threads start. `proc.wait()` blocks. Every line the service writes to stdout or stderr appears on our stdout as `api     | <line>` within milliseconds. On exit, PIPEs close, threads finish, `proc.wait()` returns, `_execute_one` joins threads (1s timeout, belt-and-suspenders) and returns.
   - Background service: same Popen + drain, but `_execute_one` returns immediately. Drain threads keep running. CLI's outer loop will `proc.wait()` after signal forwarding is set up.
4. When all foregrounds complete: `ExecutionResult` returned to CLI. If all succeeded and no background procs remain, CLI prints `"All services started"`. If background procs are alive, CLI waits on them (existing code) while drain threads continue relaying output.
5. Ctrl-C: existing signal handler sends SIGINT to process groups. Services die, PIPEs close, drain threads exit via empty-readline, main thread proceeds through kill sequence. Clean exit.

## Error handling

- **Service exits non-zero (foreground):** `shell_result.returncode = <nonzero>`. `ExecutionResult.success = False`. CLI prints `"<repo>: failed to start"` as today. The actual error output appeared inline via streaming BEFORE the summary line — no duplication needed.
- **Service crashes mid-run (background):** `proc.wait()` returns non-zero later. Existing CLI logic: other backgrounds killed, exit 1. Drain threads already relayed whatever output was produced before the crash.
- **Drain thread dies unexpectedly (e.g., I/O error):** the thread is daemon, so it doesn't block process exit. Output after the failure is lost but the main loop proceeds. Low-risk, acceptable.
- **PIPE fills up:** eliminated. Both PIPEs are drained in parallel by threads, so neither can block the child's writes.
- **Line-tearing between services:** eliminated. `StreamPrinter._lock` serializes the final `sys.stdout.write` call.
- **Partial lines at process exit:** if a service exits mid-line (no trailing `\n`), `readline()` returns the partial content; we strip+print it. Minor cosmetic edge case, not worth special handling.
- **`sys.stdout.isatty()` called inside a test:** tests using `capsys` or redirecting stdout will see `isatty() == False` → no color. That's what we want for deterministic assertions.
- **Existing `ShellResult` consumers:** a grep of the codebase for `.stdout` reads on `run` task results finds one usage in `executor.py` (healthcheck formatting). That path receives `shell_result.stdout = ""` for `run` tasks now; healthcheck reads its own message separately. Other `.stdout` reads are on non-`run` tasks (`test`, `setup`) which are unchanged. No downstream breakage.

## Testing

### Unit — `tests/util/test_stream_printer.py` (new)

- `StreamPrinter(repos=["api", "worker"]).write("api", "hello\n")` → captured stdout is `"api     | hello\n"` (with 2-space pad after name; width=6 from "worker" + 2 spaces).
- Width padding with single repo: `StreamPrinter(repos=["only"])` → prefix `"only  | "`.
- Width padding with empty list: `StreamPrinter(repos=[])` — width=0, prefix just `"<repo>  | "` (repo name still appears because it's the key). Edge case for safety; `mship run` won't hit it.
- Color enabled: `use_color=True` → prefix wrapped with ANSI escape codes (assert `\033[` present). `use_color=False` → no escape codes.
- Color auto-detect: construct in test with stdout redirected (capsys) → `isatty()==False` → no color.
- Color determinism: same repo list in any order produces the same repo→color mapping (colors assigned by sorted order).
- Thread safety: 10 threads × 100 writes each = 1000 lines. After `join()`, every captured line starts with a valid prefix (no mid-line split). Use a regex to assert every non-empty captured line matches `^<repo-padded>  | <content>$`.

### Unit — `tests/util/test_shell_streaming.py` (new)

- `drain_to_printer(fake_proc, "api", printer)` with `fake_proc.stdout = io.StringIO("line1\nline2\n")` and `fake_proc.stderr = io.StringIO("err\n")`: after joining both threads, captured stdout contains exactly three lines all prefixed `api | ...`, lines 1 and 2 from stdout in order, "err" appears once (order relative to stdout is non-deterministic due to threading — assert presence only).
- `stream = None` on one side (rare but possible if caller misuses) → thread exits quickly, no error.
- `readline()` raising on a closed stream mid-drain → caught, thread exits.

### Integration — `tests/core/test_executor_run_streaming.py` (new)

- Build a real `Executor` with a real `ShellRunner` and fake `GitRunner` / `DependencyGraph`. Config has two repos with `tasks.run` pointing to a shell command that writes to stdout and stderr: `sh -c 'echo hello; echo world >&2'`. `start_mode: foreground`.
- `executor.execute("run", repos=["api", "worker"])` with `capsys` capturing stdout.
- Assert:
  - captured stdout contains `"api"` prefix appearing at least twice (hello + world).
  - captured stdout contains `"worker"` prefix appearing at least twice.
  - All lines start at column 0 (no pre-prefix garbage).
- Repeat with `start_mode: background`: spawn the Popen, manually drain-then-kill (or have the command `sleep 0.2 && exit 0` so it exits on its own), assert same output appeared.
- Non-zero exit: `sh -c 'echo oops; exit 2'` → output captured in capsys, `result.success == False`, `result.results[0].shell_result.returncode == 2`.

### Regression — `tests/core/test_executor.py` (existing)

- Tests that exercise `setup`, `test`, and other canonical tasks still use the capture path and still read `shell_result.stdout` / `shell_result.stderr`. No changes required, but run the file to confirm no breakage.

### Manual smoke

- Scratch workspace with two repos, each with a `Taskfile.yml` run task that echoes a few lines over a couple of seconds then exits. Run `mship run`; confirm prefixed lines interleave. Ctrl-C the run; confirm clean exit. Re-run with `mship run | cat` (non-TTY stdout); confirm no ANSI escapes in the output. Finally, a background-mode repo: confirm its output also appears live while the foreground services run.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | Scope to `canonical_task == "run"` only | Other tasks (`test`, `setup`) return structured results that mship consumes. Streaming would require parallel capture which complicates the executor for no user benefit. |
| 2 | Pipe separator `<repo>  \| <line>`, not brackets or colon | Brackets false-match content (tracebacks, JSON). Colons match everywhere (URLs, timestamps). Pipe is rare in tool output and matches docker-compose convention. |
| 3 | Stdout + stderr share the same prefix | Visual distinction adds width overhead and breaks the clean split-on-pipe rule. Log level is a content-level concern. Docker-compose also merges. |
| 4 | Color per-repo via raw ANSI, not Rich | `StreamPrinter` is a tiny pure module — no reason to pull Rich into shell.py. Raw ANSI escapes are deterministic, 6 lines of code, and test-friendly (just check for `\033[` or its absence). |
| 5 | TTY auto-detection via `sys.stdout.isatty()`, no flag | `--color=always/never/auto` is a classic bikeshed. Auto covers 99% of cases; piped output is automatically clean. Can add a flag later if a real user needs it. |
| 6 | No file persistence | `.mothership/logs/<repo>.log` would be useful but out of scope. The user's Taskfile `logs` task is the existing persistence contract. Adding ours creates ambiguity. |
| 7 | `StreamPrinter` constructed once per `execute()` call, held as `self._printer` | Ensures the same width/colors apply across all tiers of a single `mship run`. Scoping to the call (not singleton) keeps tests isolated. |
| 8 | Drain threads are daemons | Don't block process exit if a background service's PIPE is slow to close. Acceptable loss: at most a few lines of late output during shutdown. |
| 9 | Foreground `_execute_one` joins drain threads with 1s timeout | Belt-and-suspenders: ensure any buffered output is flushed before the `RepoResult` is returned. 1s is well past any realistic drain time; beyond that, accept loss. |

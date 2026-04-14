# `mship test --diff` + Structured Logs

**Status:** Design approved, ready for implementation plan.
**Date:** 2026-04-14

## Purpose

Today `mship test` prints pass/fail per repo and writes the result to `task.test_results[repo].status`. Agents parse stdout. There's no memory of previous runs, no notion of "what changed since last iteration," no structured signal an agent can reliably use to decide what to work on next. Structurally similar problem for `mship log`: entries are timestamp + freeform string, so the switch-handoff's "last log in this repo" answer is correct only by luck, and the agent can't query "what's still open" or "when did this test first fail."

This spec introduces:

1. **Per-iteration test run storage** — every `mship test` run writes a structured JSON artifact with per-repo status, duration, exit code, and stderr tail. Each run has an iteration number that auto-increments per task.
2. **Test-run diffing** — `mship test` compares the current run against the previous one and labels each repo as `new failure`, `fix`, `regression`, `still failing`, `still passing`, or `first run`. This is the default output; `--no-diff` falls back to today's plain pass/fail.
3. **Structured log entries** — `LogEntry` gains optional fields (`repo`, `iteration`, `test_state`, `action`, `open_question`). Freeform `mship log "msg"` still works; new flags set structured fields explicitly.
4. **Automatic log entry after every test run** — records the iteration, summary test_state, and a structured `action: "ran tests"` entry so `mship switch` handoffs and session-resume flows have something concrete to surface.

## Non-Goals

- Per-test-case parsing (pytest / go test / jest / etc.). Future work once real adapters exist.
- Remote / distributed test result sharing. Iteration files are local.
- Long retention. Only the last N iterations are kept per task.
- Backward-incompatible log format. Old entries load fine with default `None` structured fields.
- A query DSL for structured logs. v1 adds one helper (`--show-open`); richer querying later.

## Architecture

### Test run artifacts

Iteration files live under `.mothership/test-runs/<task-slug>/<iteration>.json`. One file per run. A pointer file `.mothership/test-runs/<task-slug>/latest.json` is a symlink (or copy on non-symlink-capable systems) to the most recent.

Schema:

```json
{
  "iteration": 3,
  "started_at": "2026-04-14T12:00:00Z",
  "duration_ms": 4820,
  "repos": {
    "shared":       {"status": "pass", "duration_ms": 1200, "exit_code": 0, "stderr_tail": null},
    "auth-service": {"status": "fail", "duration_ms": 3500, "exit_code": 1, "stderr_tail": "FAIL test/auth_test.go:42\n  expected 'Bearer', got ''\n..."}
  }
}
```

`stderr_tail` is the last 40 lines of stderr, only populated when `status == "fail"`. Capped at ~4 KB per repo to keep files cheap to read.

Retention: keep the newest 20 files per task. Pruning runs at the top of every `mship test` after the new file is written. `mship close` removes the whole `.mothership/test-runs/<task-slug>/` directory as part of its existing cleanup.

### Iteration counter

New optional field `Task.test_iteration: int = 0`. Incremented once per `mship test` invocation (after the run completes). First run becomes iteration 1.

### `mship test` flow

1. Increment `task.test_iteration` (tentatively — actual persist happens at step 5).
2. Run tests as today, but the executor captures `duration_ms` per repo and returns stderr alongside the existing `ShellResult`.
3. Write `.mothership/test-runs/<task-slug>/<iteration>.json`.
4. Update `latest.json` pointer.
5. Persist the new `test_iteration` + `task.test_results[repo]` entries (existing behavior).
6. Prune iteration files beyond the newest 20.
7. Auto-append a structured log entry:
   ```
   LogEntry(
       timestamp=now,
       message="iter N: {pass_count}/{total} passing",
       repo=None,                # test run spans multiple repos
       iteration=N,
       test_state=<"pass"|"fail"|"mixed">,
       action="ran tests",
       open_question=None,
   )
   ```
   `test_state`: `pass` if all pass, `fail` if all fail, `mixed` otherwise.
8. Render the diff output (see below).

### Diff computation

Load the previous iteration file (iteration N−1) if it exists. For each repo in the current run:

- No previous file or repo not in previous → `first run`.
- Previous status and current status:
  - `pass` → `pass` → `still passing`
  - `fail` → `pass` → `fix`
  - `pass` → `fail` → check: was this repo `pass` in *both* iterations N−2 and N−1? If yes → `regression`. If N−2 doesn't exist or repo was `fail` in N−2 → `new failure`.
  - `fail` → `fail` → `still failing`

Repos in previous but not current (e.g. `--repos` filter narrowed the run): not shown in the diff. Not an error.

### TTY output

```
Test run #3  (4.8s)
  shared:       pass   (1.2s)
  auth-service: fail   (3.5s)  ← new failure
    FAIL test/auth_test.go:42
      expected 'Bearer', got ''

  3/4 repos passing. 1 new failure since iter #2.
```

`stderr_tail` is shown indented under a failed repo, truncated to 20 lines in TTY mode (full 40 lines stay in the JSON file). Previous-iteration context appears in the footer: `new failure since iter #2`, `fix since iter #1`, etc.

`--no-diff` falls back to the current simple `shared: pass / auth-service: fail` format without labels or stderr inline.

### JSON output

When not a TTY, `mship test` emits the current iteration file's content verbatim, plus a `diff` object:

```json
{
  "iteration": 3,
  "started_at": "...",
  "duration_ms": 4820,
  "repos": { ... },
  "diff": {
    "previous_iteration": 2,
    "tags": {
      "shared":       "still passing",
      "auth-service": "new failure"
    },
    "summary": {
      "new_failures": ["auth-service"],
      "fixes": [],
      "regressions": [],
      "new_passes": []
    }
  }
}
```

When there's no previous iteration, `diff.previous_iteration` is `null`, tags are all `first run`, and summary arrays are empty.

### `--timing` flag (deferred but cheap to add later)

Not shipped in v1. `--timing` would flag repos whose `duration_ms` grew by >2× since the previous run. Deferred — the JSON file has the data; add the flag when someone asks.

## Structured Logs

### `LogEntry` shape

```python
@dataclass
class LogEntry:
    timestamp: datetime
    message: str
    repo: str | None = None
    iteration: int | None = None
    test_state: Literal["pass", "fail", "mixed"] | None = None
    action: str | None = None
    open_question: str | None = None
```

### Storage format

Keep the markdown log (`<task-slug>.md`) as today. Extend the entry header to carry structured fields as a subset of YAML frontmatter, preserved in-place so humans can still read the file:

```markdown
# Task Log: add-labels

## 2026-04-14T12:00:00Z  repo=shared  iter=3  test=pass  action=implementing
Implemented Label type with workspace_id in types.ts.

## 2026-04-14T12:05:00Z  action="ran tests"  iter=3  test=mixed
iter 3: 3/4 passing

## 2026-04-14T12:07:00Z  repo=auth-service  open="how should null workspace map?"
Stuck on null-workspace handling in middleware.
```

Parser updates the existing regex to tolerate both the old format (`## <ts>` with no kv pairs) and the new format (`## <ts>  k=v  k="quoted v"  ...`). Old entries load with every structured field set to `None`.

### `mship log` CLI

- `mship log "message"` — same as today. New fields default to `None` except:
  - `repo` is inferred from `task.active_repo` (if set).
  - `iteration` is set from `task.test_iteration` (if > 0).
- `mship log "msg" --action "..." --open "..." --test-state pass --repo shared --iteration 5` — any subset explicitly provided wins over inferred values. `--no-repo` or `--repo ""` explicitly clears the inferred repo.
- `mship log --show-open` — scan this task's log entries and print those with `open_question is not None`:
  ```
  Open questions:
    [2h ago] auth-service: how should null workspace map?
  ```
  Exit 0 even if none.
- `mship log --last N` — already exists; unchanged.

### Inference defaults

Kept simple — automatic inference only where the value is unambiguous:
- `repo` → `task.active_repo` when unset.
- `iteration` → `task.test_iteration` when unset and > 0.

No automatic `action` or `test_state` inference. If the user wants them, they pass flags.

### `mship switch` handoff accuracy

`build_handoff`'s `last_log_in_repo` filter becomes: most recent log entry whose `repo` field equals the target repo. If none, fall back to the most recent entry overall (current behavior). Once agents start tagging entries, switch handoffs get dramatically more relevant.

### JSON output

`mship log` and `mship log --last N` JSON output gains the new fields:

```json
{
  "task": "add-labels",
  "entries": [
    {
      "timestamp": "2026-04-14T12:00:00Z",
      "message": "Implemented Label type...",
      "repo": "shared",
      "iteration": 3,
      "test_state": "pass",
      "action": "implementing",
      "open_question": null
    }
  ]
}
```

## Data Model Changes

- `src/mship/core/log.py`:
  - `LogEntry` gains 5 optional fields.
  - `_parse` extended to tolerate `k=v  k="v with spaces"` header segments.
  - `append(...)` signature gains keyword-only parameters for the structured fields.

- `src/mship/core/state.py`:
  - `Task` gains `test_iteration: int = 0`.

- `src/mship/core/executor.py`:
  - `RepoResult` gains `duration_ms: int = 0` (new attribute; set by the runner).
  - The test path in `RepoExecutor` times each repo's run via `time.monotonic()` deltas.

- `src/mship/core/test_history.py` (new):
  - `write_run(state_dir, task_slug, iteration, started_at, duration_ms, results) -> Path`
  - `read_run(state_dir, task_slug, iteration) -> dict | None`
  - `latest_iteration(state_dir, task_slug) -> int | None`
  - `compute_diff(current_run, previous_run, pre_previous_run) -> dict` — applies the tagging rules above.
  - `prune(state_dir, task_slug, keep=20)`

## CLI Surface Changes

- `mship test`:
  - New flag `--no-diff` (default off). Diff shown by default after first run; first run shows plain output.
- `mship log`:
  - New flags: `--action`, `--open`, `--test-state`, `--repo`, `--no-repo`, `--iteration`, `--show-open`.

## Error Handling

- Previous iteration file missing or corrupt → treat as first run (no diff labels). Warn in stderr, continue.
- Test run where the executor fails before producing results → no iteration file written, no log entry appended, `task.test_iteration` not incremented. Error surfaces as today.
- Log file parse failure on old entries → skip the malformed entry, continue (same as today's tolerance).
- `mship log --show-open` on task with no log file → empty output, exit 0.

## Testing

### Unit tests (`tests/core/test_test_history.py` — new)

- `write_run` creates `<iteration>.json` + `latest.json` pointer.
- `compute_diff` correctly tags every combination: first run, pass→pass, pass→fail (with/without N−2 context), fail→pass, fail→fail.
- `prune` keeps the newest 20, deletes older files, skips `latest.json`.

### Unit tests (`tests/core/test_log.py` — extend)

- `LogEntry` accepts all new fields; defaults are `None`.
- `_parse` round-trips the new kv-header format.
- `_parse` tolerates mixed old + new entries in the same file.
- `append(..., repo="shared", action="x", open_question="y")` writes a new-format header.

### Integration tests (`tests/test_exec_integration.py` — extend, or new `tests/test_test_diff_integration.py`)

- First `mship test` after spawn: no diff labels, iteration file written, `task.test_iteration == 1`, log entry appended with `action="ran tests"` and correct `test_state`.
- Second `mship test` with same results: `still passing` / `still failing` labels, iteration 2 written, N−1 persists.
- Change a repo from pass to fail between runs → `new failure` label, stderr_tail present in the JSON.
- Three runs with pass → pass → fail → `regression` label (requires N−2 history).
- `mship test --no-diff`: no labels printed; iteration file still written.
- `mship log --action X --open Y` writes a new-format entry; `mship log` reads it back with structured fields populated.
- `mship log --show-open` on a task with one open question prints it; on a task with none exits 0 silently.
- `mship log` without explicit `--repo` after `mship switch shared` auto-infers `repo=shared`.
- `mship switch` handoff's `last_log_in_repo` prefers an entry tagged with the switched-to repo over an older untagged entry.

### Retention test

- Write 25 runs; assert only 20 remain after pruning, all the newest.

## Out of Scope (post-v1)

- Per-test-case parsing via language adapters (pytest, go, jest, …). When someone asks, add a `mship test --parse` flag that tries known adapters.
- `--timing` flag flagging >2× slowdowns.
- `mship log --since <iteration>` and `mship log --action-matches <regex>` query helpers.
- Automatic `action` inference from the shell command that just ran.
- Publishing iteration files to a shared store for multi-agent coordination (v2 territory).

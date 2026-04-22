# Task Breadcrumb + Ambiguity Detection — Design

Closes #77.

## Problem

When multiple tasks are active, commands resolve their target through cwd → `MSHIP_TASK` env → `--task` flag, with a fallback to the sole active task when exactly one exists. It works, but nothing tells the user which task the command actually landed on, or why. Two real failure modes:

1. Cwd drifts to a different task's worktree between commands; subsequent commands silently switch context.
2. A user runs a command from a non-worktree dir with one active task and gets the single-task auto-resolve. They didn't anchor; mship picked for them; they never know.

A subagent dispatched on the wrong task is the worst case — tests pass, but against the wrong branch.

## Solution

1. Print a one-line breadcrumb at the start of every task-scoped state-changing command, naming the resolved task and the resolution source.
2. Tighten ambiguity detection: if cwd sits inside multiple worktree paths, fail with candidate `--task` hints rather than silently picking the first match.
3. Structured callers (non-TTY / JSON mode) receive `resolved_task` and `resolution_source` fields in the command's JSON payload instead of a human-readable line.

## Scope

**In scope (per Bailey's decision — option C):** state-changing verbs + commands that feed subagent sessions. Concretely: `finish`, `close`, `phase`, `dispatch`, `exec`, `switch`, `block`, `context`, `log` (`log append` writes journal entries — state change).

**Out of scope:** view commands (`mship status`, `mship logs`, `mship diff`, `mship spec`, view subcommands). They already name the task in their primary output; a breadcrumb would be redundant noise.

**Explicitly deferred:** global `--quiet` flag. The issue mentions it as an alternative trigger. V1 gates on `is_tty` only. TTY users who find breadcrumbs noisy can redirect stderr to `/dev/null`. If demand arises, add `--quiet` later as a plain flag on each in-scope command.

## Architecture

One new enum, one resolver API change, one CLI helper, nine migrated call sites.

```
mship.core.task_resolver
  ├─ ResolutionSource (enum)     ← NEW
  └─ resolve_task() -> (Task, ResolutionSource)   ← signature change

mship.cli._resolve
  ├─ resolve_or_exit(state, cli_task) -> Task     ← existing, unchanged
  └─ resolve_for_command(cmd, state, cli_task, output) -> Task   ← NEW

mship.cli.{worktree,phase,dispatch,exec,switch,block,context,log}
  └─ finish/close/phase/dispatch/exec/switch/block/context/log
     └─ one-line swap: resolve_or_exit → resolve_for_command
```

## Resolver change

`ResolutionSource` is a `StrEnum` with four values. The string value is what lands in the breadcrumb and the JSON payload.

```python
class ResolutionSource(StrEnum):
    CLI_FLAG = "--task"
    ENV_VAR = "MSHIP_TASK"
    CWD = "cwd"
    SINGLE_ACTIVE = "only active task"
```

The resolver's four-branch control flow maps directly to these sources. All three fallback paths (flag / env / cwd) produce distinct sources; the "exactly one active task, no anchor" case gets `SINGLE_ACTIVE` so the breadcrumb is honest about what happened:

```
→ task: add-labels  (resolved via only active task)
```

`resolve_task` becomes `(Task, ResolutionSource)`. Every caller (the existing `resolve_or_exit` and the new `resolve_for_command`) unpacks explicitly. This is a breaking change to the internal API but all internal callers live under `src/mship/cli/` and `src/mship/core/`. No external tooling imports this function.

### Ambiguity detection upgrade

Today the cwd walk returns the first match silently when cwd is inside multiple worktree paths. This is possible with nested checkouts (worktree inside a worktree) or symlinked worktree roots — rare but real, and silent selection is the worst possible behavior.

New behavior: the cwd-walk collects all matches. If ≥2, raise `AmbiguousTaskError` with a new `candidates: list[tuple[str, Path]]` attribute (task slug + worktree path). The CLI helper renders:

```
ambiguous task: cwd is inside 2 worktree paths.
Pick one with --task:
  --task add-labels  (/path/to/worktrees/feat/add-labels)
  --task fix-bug     (/path/to/worktrees/feat/fix-bug)
```

The existing `AmbiguousTaskError` case (multiple tasks, no anchor, cwd outside all worktrees) also gets the `candidates` list populated — with ALL active tasks — so the error message can list concrete `--task` invocations regardless of the ambiguity flavor.

## CLI helper

`mship.cli._resolve.resolve_for_command(cmd_name, state, cli_task, output) -> Task`.

Behavior:

1. Calls `resolve_task`. Catches the three exception types:
   - `NoActiveTaskError`, `UnknownTaskError` — reuse existing messages (no regression).
   - `AmbiguousTaskError` — print the new candidate list when `candidates` is populated.
2. On success, in TTY mode: print `→ task: <slug>  (resolved via <source>)` to **stderr** before any command output. Stderr so stdout stays clean for pipes; `2>/dev/null` suppresses for users who find it noisy.
3. In non-TTY mode: no breadcrumb printed. The command's existing JSON emitter is responsible for including `resolved_task` and `resolution_source` in its payload — the helper returns the task; each in-scope command adds two fields to its own `output.json({...})` call.
4. Returns the `Task`.

The existing `resolve_or_exit` stays for out-of-scope commands. No breaking change to view commands.

## Migration

Nine call sites, each a one-line edit:

| Command | File | Current | New |
|---|---|---|---|
| `finish` | `cli/worktree.py` | `resolve_or_exit(state, task)` | `resolve_for_command("finish", state, task, output)` |
| `close` | `cli/worktree.py` | same | `resolve_for_command("close", ...)` |
| `phase` | `cli/phase.py` | same | `resolve_for_command("phase", ...)` |
| `dispatch` | `cli/dispatch.py` | same | `resolve_for_command("dispatch", ...)` |
| `exec` | `cli/exec.py` | same | `resolve_for_command("exec", ...)` |
| `switch` | `cli/switch.py` | same | `resolve_for_command("switch", ...)` |
| `block` | `cli/block.py` | same | `resolve_for_command("block", ...)` |
| `context` | `cli/context.py` | same | `resolve_for_command("context", ...)` |
| `log` | `cli/log.py` | same | `resolve_for_command("log", ...)` |

Each command also gets two new fields in its non-TTY JSON output path: `resolved_task` (the slug) and `resolution_source` (the enum value).

## JSON contract

Every in-scope command's `output.json(...)` payload gains:

```json
{
  "resolved_task": "add-labels",
  "resolution_source": "cwd",
  ... existing fields ...
}
```

Scripts / agent wrappers that parse this output can branch on `resolution_source` to warn when SINGLE_ACTIVE was the fallback.

## Testing

### Unit — `tests/core/test_task_resolver.py`

- `resolve_task` returns `ResolutionSource.CLI_FLAG` when `cli_task` is passed.
- Returns `ENV_VAR` when `env_task` is set.
- Returns `CWD` when cwd is inside a worktree.
- Returns `SINGLE_ACTIVE` on the one-task fallback.
- `AmbiguousTaskError.candidates` is populated for both the cwd-inside-multiple case and the no-anchor-multiple-tasks case.
- Cwd walk raises `AmbiguousTaskError` (not silent return) when ≥2 worktree matches.

### Integration — `tests/cli/test_breadcrumb.py` (new)

- Parametrized over in-scope commands + each resolution source: invocation prints the breadcrumb with the expected source on stderr.
- Non-TTY invocation of one representative command emits `resolved_task` + `resolution_source` in JSON payload and nothing on stderr.
- Ambiguity: CLI invocation with two tasks and cwd outside both exits non-zero and the error lists `--task <slug>` hints for each active task.

### Regression

- Existing tests for `resolve_or_exit` keep passing (signature unchanged for out-of-scope commands).
- Existing behavior tests for finish / close / phase / dispatch keep passing (only the resolution call changes; business logic untouched).

## Anti-goals

- No new CLI flag for `--quiet`. V1 gates on `is_tty`.
- No changes to read-only view commands. They already show the task.
- No automatic task-switching. The breadcrumb is a signal, never a redirect.
- Not a replacement for `mship status`. Breadcrumb is one line; status is the full picture.

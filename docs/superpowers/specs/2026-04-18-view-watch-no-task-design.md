# `view journal` / `view spec` — survive no-task in `--watch` mode — Design

## Context

`mship view journal --watch` and `mship view spec --watch` exit 1 immediately when the workspace has no active task, instead of rendering a placeholder and polling until a task appears. This breaks the intended pane-first workflow: users launch a zellij/tmux layout with these views already running, then `mship spawn` a task, and expect the panes to pick up the task automatically.

Reproduced on the installed build (post-#62):

```
$ mship view journal --watch
ERROR: no active task; run `mship spawn "description"` to start one
$ echo $?
1
```

`view status --watch` and `view diff --watch` already handle the empty-workspace case gracefully (placeholder text, keep polling). The bug is scoped to `view journal` and `view spec` because both commands call `resolve_or_exit()` in the CLI entry handler, which hard-exits before the Textual view ever mounts.

## Goal

In `--watch` mode, `view journal` and `view spec` mount successfully even when the workspace has no active task (or has multiple active tasks with no anchor, or has been started with `--task <slug>` for a slug that doesn't exist yet). The view renders a short placeholder and re-resolves the task on every tick, so that when a task becomes resolvable the view starts rendering normal content without a restart.

Non-watch behavior is unchanged: `mship view journal` (no `--watch`) with no active task still exits 1 via the existing `resolve_or_exit` path. Scripts that grep the exit code continue to work.

## Success criterion

From a workspace with no active task:

```
$ mship view journal --watch    # in one pane, stays open
$ mship view spec --watch       # in another pane, stays open
```

Both panes display:

```
No active task. Run `mship spawn "description"` to start one.
```

and poll every 2s. In another terminal:

```
$ mship spawn "foo"
```

Within one tick (≤2s), the journal pane flips to the task's journal and the spec pane flips to the spec-fallback rendering (task description + recent journal, because no spec file exists yet). Neither pane is restarted.

If the user then `mship spawn "bar"` (making the workspace have two active tasks with no anchor), both panes flip to:

```
Multiple active tasks (bar, foo). Pass --task, set MSHIP_TASK, or close extras.
```

If the user `mship close`s one task, the panes resolve to the survivor automatically.

If the pane was started with `--task foo` and `foo` does not exist yet, the placeholder reads:

```
Task 'foo' not found. Waiting for it to be spawned.
```

## Anti-goals

- **No change to `view status` / `view diff`.** They already handle the empty-workspace case; no reason to touch them. Extending the same pattern to `view status --task <slug>` (which still uses `resolve_or_exit` and would hard-exit on unknown slug in watch mode) is a separate follow-up if it turns out to be a real problem.
- **No change to non-watch behavior.** `mship view journal` without `--watch` and with no active task continues to write an error to stderr and exit 1. Scripts and layouts that depend on the exit-code contract keep working.
- **No change to the `view spec <name-or-path>` path.** When an explicit spec name or path is provided, task resolution is already skipped. That path stays unchanged.
- **No retry backoff, no rate limiting.** The existing 2s polling interval is fine; resolver calls are cheap.
- **No new CLI flags.** The fix is transparent — `--watch` simply becomes tolerant.

## Behavior

| Scenario | Non-watch | Watch (new) |
|---|---|---|
| 0 tasks | stderr error, exit 1 | placeholder: "No active task. Run \`mship spawn "description"\` to start one." |
| 1 task resolves cleanly | render content | render content |
| 2 tasks, no anchor | stderr error, exit 1 | placeholder: "Multiple active tasks (slugs). Pass --task, set MSHIP_TASK, or close extras." |
| `--task foo`, `foo` missing | stderr error, exit 1 | placeholder: "Task 'foo' not found. Waiting for it to be spawned." |
| Any of the above then resolves later | N/A (single render) | view flips to content on the next tick |

Resolution is re-evaluated every tick in watch mode. The view does not lock onto a slug once it finds one — if the task gets closed, the pane flips back to the placeholder; if a new task spawns, the pane picks it up. This matches the behavior of `view status --watch` (always reflects current state).

## Architecture

### New module — `src/mship/cli/view/_placeholders.py`

A single helper that maps resolver exceptions to placeholder strings. Keeps wording in one place so tests and both views share the same copy.

```python
from mship.core.task_resolver import (
    AmbiguousTaskError, NoActiveTaskError, UnknownTaskError,
)

def placeholder_for(err: Exception) -> str:
    if isinstance(err, NoActiveTaskError):
        return 'No active task. Run `mship spawn "description"` to start one.'
    if isinstance(err, AmbiguousTaskError):
        return (
            f"Multiple active tasks ({', '.join(err.active)}). "
            "Pass --task, set MSHIP_TASK, or close extras."
        )
    if isinstance(err, UnknownTaskError):
        return f"Task '{err.slug}' not found. Waiting for it to be spawned."
    raise err
```

### `LogsView` changes — `src/mship/cli/view/logs.py`

New constructor params:
- `cli_task: str | None` — the raw `--task` string (used only in watch mode for re-resolution).
- `cwd: Path` — captured at construction; pane cwd doesn't change during the view's lifetime.

Existing `task_slug: Optional[str]` stays: the CLI passes it in non-watch mode (pre-resolved) and passes `None` in watch mode.

New private method:

```python
def _resolve_slug(self) -> str:
    """Return the task slug to render for this tick.

    Non-watch: returns self._task_slug (pre-resolved by CLI). Watch:
    re-resolves each call; resolver errors propagate.
    """
    if self._task_slug is not None:
        return self._task_slug
    state = self._state_manager.load()
    task = resolve_task(
        state,
        cli_task=self._cli_task,
        env_task=os.environ.get("MSHIP_TASK"),
        cwd=self._cwd,
    )
    return task.slug
```

`gather()` wraps the existing body in a `try/except (NoActiveTaskError, AmbiguousTaskError, UnknownTaskError)` around `_resolve_slug()`. On caught error: `return placeholder_for(err)`. On success: read `active_repo` from the resolved task (via a second fresh state load — cheap and keeps scoping up to date), then format entries as today.

### `SpecView` changes — `src/mship/cli/view/spec.py`

New constructor params:
- `state_manager` — replaces the pre-loaded `state` param.
- `cli_task: str | None`.
- `cwd: Path`.

`_refresh_content()` in watch mode (or any time `name_or_path is None`):
1. `state = self._state_manager.load()`.
2. Try `resolve_task(state, cli_task=self._cli_task, env_task=os.environ.get("MSHIP_TASK"), cwd=self._cwd)`. Resolver error → `Markdown.update(placeholder_for(err))`, clear `error_static`, return (after scroll-preserve).
3. Success → existing flow: `find_spec(workspace_root, None, task=slug, state=state)`, handle `SpecNotFoundError` via `_render_task_fallback(slug, state, default_error=str(e))`. The fallback now takes `state` as a parameter rather than reading `self._state` (stays fresh per tick).

When `name_or_path` is set (explicit spec), resolution is skipped and the existing behavior is preserved.

### CLI handler changes

Both handlers — `view journal` (in `src/mship/cli/view/logs.py`) and `view spec` (in `src/mship/cli/view/spec.py`) — split on `watch`:

```python
if watch:
    task_slug = None
    cli_task = task            # raw --task string
else:
    t = resolve_or_exit(state, task)
    task_slug = t.slug
    cli_task = None
```

Pass both `task_slug` and `cli_task` into the view, plus `state_manager` and `cwd=Path.cwd()`. Non-watch sets `task_slug`, `cli_task=None`. Watch sets `task_slug=None`, `cli_task=<the flag>`.

For `view spec`: if `name_or_path is not None`, skip the resolver entirely (unchanged). If `name_or_path is None`, branch on `watch` as above.

## Non-watch scripting contract (preserved)

Today's behavior:
- `mship view journal` with no active task: stderr error, exit 1.
- `mship view spec` with no active task: stderr error, exit 1.
- `mship view journal --task foo` where `foo` is unknown: stderr error, exit 1.

All of these still exit 1 after this change — only the `--watch` code path becomes tolerant. This is enforced by a new regression test (below).

## Data flow

**`view journal --watch`, tick fires:**
1. `LogsView._refresh_content()` → `gather()`.
2. `gather()` calls `_resolve_slug()`. On resolver error → `return placeholder_for(err)`.
3. On success: read task, pull `active_repo` for scoping, read journal entries, format and return.
4. `_refresh_content()` updates the `Static` widget; scroll-preservation logic unchanged.

**`view spec --watch` (no explicit `name_or_path`), tick fires:**
1. `SpecView._refresh_content()` reloads state via `state_manager.load()`.
2. Resolves slug. Resolver error → `Markdown.update(placeholder_for(err))`, clear `error_static`, restore scroll.
3. Success → `find_spec(...)` with the fresh state. If a spec file exists, render it. If `SpecNotFoundError`, render `_render_task_fallback(slug, state, ...)`.

## Testing

### `tests/cli/view/test_placeholders.py` (new)

- `NoActiveTaskError` → exact expected string.
- `AmbiguousTaskError(active=["a", "b"])` → string contains `"a, b"` and the three possible remedies.
- `UnknownTaskError(slug="foo")` → string contains `"foo"`.
- Unknown exception type → re-raised.

### `tests/cli/view/test_logs_view.py` (extend)

Fakes already exist (`_FakeStateMgr`, `_FakeLogMgr`). Add:

- **Watch + empty state:** construct `LogsView(task_slug=None, cli_task=None, watch=True, state_manager=FakeStateMgr(no_tasks), ...)`. Run test, assert rendered text contains `"No active task."`.
- **Watch + ambiguous:** state has 2 tasks, no `cli_task`, no `MSHIP_TASK`, cwd outside any worktree. Rendered text contains `"Multiple active tasks"` and both slugs.
- **Watch + unknown slug:** `cli_task="foo"`, state has tasks but none with slug `foo`. Rendered text contains `"Task 'foo' not found."`.
- **Watch + transition:** state starts empty; mutate fake to add a task; `pilot.pause()` again; assert the second rendered text is journal entries, not the placeholder.
- **Non-watch with `task_slug` set:** existing entry-rendering test stays green; new regression guard asserts the resolver is never called in this path.

### `tests/cli/view/test_spec_view.py` (extend)

Same four cases (no-active, ambiguous, unknown-slug, transition). Additional cases:

- **Watch + task exists + no spec file:** rendered markdown contains the task-fallback header (`# No spec yet for task`).
- **Watch + task exists + spec file exists:** rendered markdown equals the spec's source.
- **Explicit `name_or_path`:** unchanged — resolution skipped; rendering always succeeds or shows the existing "Spec not found" error.

### `tests/cli/view/test_view_cli.py` (new)

Typer CliRunner-level tests. Tests spawn the app but exit fast (either via non-watch single render, or `pilot.pause()` + explicit quit in watch mode).

- **Non-watch regression:** `runner.invoke(app, ["view", "journal"])` with no active task exits 1 with the existing stderr error.
- **Non-watch regression:** `runner.invoke(app, ["view", "spec"])` with no active task exits 1.
- **Watch tolerates no-task:** `runner.invoke(app, ["view", "journal", "--watch"])` — mount the app, pause, read the static widget, confirm placeholder, quit. Exit code 0.
- **Watch tolerates no-task (spec):** same for `view spec --watch`.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | Only `view journal` and `view spec` change | `view status` and `view diff` already handle empty state. Touching them would be scope creep. `view status --task <unknown>` may have a related bug but wasn't in the reported scope. |
| 2 | Preserve non-watch exit 1 | Scripts and layouts depend on the exit-code contract. The reported bug is specifically about `--watch`; flipping non-watch behavior would be silent breakage. |
| 3 | Re-resolve every tick (vs. lock on first success) | Matches `view status --watch`'s "always reflect current state" model. Handles the "close one of two tasks mid-watch" case for free. Simpler than a lock + invalidation protocol. |
| 4 | Placeholder for Ambiguous and Unknown, not only NoActive | The pane's job is "wait until there's renderable content." All three resolver errors are equivalent from the pane's perspective: nothing to render yet. Using the same mechanism for all three keeps the code simple and the behavior predictable. |
| 5 | New `_placeholders.py` module rather than inline strings | Tests assert against the same source of wording the views render. Copy drift is a repeated cause of test flakes in this codebase. |
| 6 | `SpecView` drops the pre-loaded `state` param in favor of a `state_manager` ref | Stale state after the first tick is a correctness bug in watch mode. Passing the manager is also simpler than maintaining a "refresh state" back-channel. |
| 7 | Capture `cwd` at view construction, not per tick | Pane cwd doesn't change while the view runs. Reading `Path.cwd()` per tick would be wasted syscalls and could give confusing results if the view were ever embedded in a process that `chdir`s. |

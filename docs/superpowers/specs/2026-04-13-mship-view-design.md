# `mship view` — Live Monitoring Views

**Status:** Design approved, ready for implementation plan.
**Date:** 2026-04-13

## Purpose

A new read-only presentation layer for `mship` designed to be composed into tmux/zellij layouts so humans can monitor workspace state, diffs, logs, and specs live while agents work. Each view is a single-purpose process that owns one terminal; multi-pane layout is the user's responsibility (zellij/tmux), not `view`'s.

`mship view` does not replace existing commands (`mship status`, `mship log`) — those remain for scripting. `view` is optimized for humans watching panes.

## Non-Goals

- Built-in multi-pane TUI with tabs/splits. Users compose layouts in their terminal multiplexer.
- Write operations. `view` is strictly read-only.
- Replacing `mship status` / `mship log`. Those stay.
- Event-driven refresh for every view in v1. Polling is the default; specific views can upgrade later.

## Architecture

New top-level Typer sub-app registered from `src/mship/cli/view/__init__.py`:

```
src/mship/cli/view/
    __init__.py      # Typer sub-app, registers subcommands
    _base.py         # Shared: refresh loop, key bindings, alt-screen, no-yank scroll
    status.py        # `mship view status`
    logs.py          # `mship view logs [exec-id] [--all]`
    diff.py          # `mship view diff`
    spec.py          # `mship view spec [name-or-path] [--web]`
```

Each view is a small `textual.App` subclass. Data-gathering logic lives in `src/mship/core/` — views call into core and never embed business logic. Any new helpers (untracked-diff synthesis, spec discovery, log tailing) go in core and are unit-testable without a TUI.

### Dependencies

- Add `textual` to `pyproject.toml` dependencies. Built on existing `rich`.
- Optional runtime detection of `delta` binary (no dep added).
- `--web` uses stdlib `http.server` + `markdown-it-py` (already transitively present via `rich`, verify and add explicitly if not).

## Common Behavior

All views share:

- **Flags:** `--watch` (enables refresh loop), `--interval N` (default 2s, only meaningful with `--watch`), `--no-alt-screen` (stream-to-stdout for piping/recording).
- **Alternate screen buffer** on by default. When the view exits, terminal scrollback is intact.
- **Key bindings:** `q` / `Ctrl-C` quit; `j`/`k`, `↑`/`↓`, `PgUp`/`PgDn`, `Home`/`End` scroll; `r` force refresh; `/` search (where applicable).
- **No-yank refresh:** when new data arrives, if the user has scrolled away from the edge, stay put. Auto-follow only when pinned to the end (tail-style).
- **Without `--watch`:** render once and exit. Useful for scripting, screenshots, piping.

## The Views

### `mship view status`

Workspace snapshot for a pane: phases, worktrees, running execs (with PIDs), blocked items, healthcheck states. Roughly the info of `mship status` reformatted:

- Sticky header with workspace root, active phase.
- Scrollable body grouped by phase; each phase shows its repos/worktrees/execs.
- Color-coded states (running, blocked, failed, healthy).

Data source: `core/state.py`, `core/phase.py`, `core/healthcheck.py`. Refresh: poll at `--interval`.

### `mship view logs`

Tail of the current task log (same source as `mship log`) — the human/agent message stream written by `LogManager`.

- `mship view logs` — tails the current task's log.
- `mship view logs <task-slug>` — tails a specific task's log.

Refresh: poll the log store at `--interval`, render new entries as they arrive. When new entries appear and the user is pinned to the bottom, auto-follow; otherwise stay put.

**Out of scope for v1:** tailing background-process stdout. The executor does not currently persist per-exec stdout to files; adding that capture is tracked separately and will land before `view logs` gains an exec-log mode.

### `mship view diff`

Git diff across all worktrees in the workspace. One collapsible section per worktree with a header like:

```
▶ repo-a (worktree: feat-x) · 3 files · +42 -11
```

**Untracked files are rendered inline with modified files, expanded by default**, as synthesized "new file" diffs (every line a `+` addition). No git index mutation — synthesis is done by reading the file and constructing the diff hunk ourselves. Untracked files are discovered via `git ls-files --others --exclude-standard` so `.gitignore` is respected. Binary new files render as a one-line `new binary file, N bytes` stub.

Rendering: if `delta` is on `PATH`, pipe through it; otherwise fall back to `rich` syntax highlighting. Fallback mentions `delta` once in the footer.

Refresh: poll at `--interval`. May be upgraded to filesystem-event-driven refresh post-v1.

### `mship view spec`

Renders a spec file using Textual's `Markdown` widget.

- `mship view spec` — newest file in `docs/superpowers/specs/` by mtime.
- `mship view spec <name-or-path>` — specific file. `<name>` resolves against `docs/superpowers/specs/` with or without `.md`.
- `--web` — starts a local `http.server` serving the rendered HTML, opens the browser, prints the URL. Does not occupy the terminal with a TUI when `--web` is used (it just serves + watches).

Refresh: file-mtime watch at `--interval`. Re-renders when the file changes.

**Port selection for `--web`:** default start port is `47213` (uncommon range). Skip these known dev ports while scanning upward: `3000, 3001, 4200, 5000, 5173, 8000, 8080, 8888, 9000`. Try up to 10 ports; error out if none free. `--port N` flag to override.

## Error Handling

Views must degrade, never crash the pane:

- Missing `mothership.yaml` or state dir: render centered message ("No mothership workspace here"), keep polling; recover if it appears.
- Spec dir empty or requested spec missing: friendly message naming the path checked.
- Log file rotated or deleted mid-tail: reopen on next poll.
- `delta` not on `PATH`: silent fallback to `rich`; one-line footer note.
- `--web` port range exhausted: error and exit with a clear message.
- Worktree removed while `view diff` is running: drop its section on next refresh.

## Testing

- **Unit tests** (pure functions, no TUI):
  - Untracked-file diff synthesis (text, empty, binary, respects `.gitignore`).
  - Spec discovery (newest-by-mtime, name resolution with/without extension, missing dir).
  - Log tailer (initial read, delta reads as new entries appear, handles missing task gracefully).
  - Web port scan (skips blocklist, respects `--port`, fails cleanly when exhausted).
- **TUI tests** using Textual's `App.run_test()` / `Pilot`:
  - Mount each view with fake data, step the refresh loop, assert rendered text.
  - Assert scroll position is preserved across refresh when user has scrolled away from the edge.
  - Assert auto-follow behavior when pinned to the end.
- No real-terminal end-to-end tests — too flaky for what they buy.

## Out of Scope (post-v1 candidates)

- Event-driven refresh (inotify/fsevents) for `diff` and `logs`.
- Additional views (`phase`, `healthcheck`, `worktree`).
- A bundled zellij/tmux layout file (`mship view layout` command).
- In-view actions (e.g. `b` to block a phase from `view status`).

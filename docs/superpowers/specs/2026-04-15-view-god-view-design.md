# Design — `mship view` God view across all tasks & worktrees

**Status:** Approved
**Date:** 2026-04-15
**Author:** Bailey Seymour (with Claude)

## Problem

`mship view` subcommands operate only on the current task and the main checkout. Specs are now created inside task worktrees (under `<worktree>/docs/superpowers/specs/`), but `view spec` only scans `<workspace_root>/docs/superpowers/specs/` — so specs in sibling worktrees are invisible. `view status` shows the current task only; there is no single pane that surveys every task in flight.

The user wants a **God view**: every `view` subcommand should default to an all-tasks view, with `--task <slug>` to narrow.

## Goals

- All four `view` subcommands (`status`, `spec`, `logs`, `diff`) default to a cross-task view.
- `view spec` finds specs in every task's worktrees and in the main checkout.
- `--task <slug>` narrows any view to a single task.
- Finished-awaiting-close tasks remain visible with a clear marker.
- Existing zellij/tmux panes don't break — new picker replaces the "no active task" empty state with useful information.

## Non-Goals

- No changes to the state model. State already carries everything needed.
- No interleaved live-logs across tasks (may add later as `logs --merge`).
- No cross-workspace God view — scope is within one mothership workspace.

## Architecture

### Section 1 — Data layer (`mship/core/view/task_index.py`, new)

```python
@dataclass(frozen=True)
class TaskSummary:
    slug: str
    phase: str
    branch: str
    affected_repos: list[str]
    worktrees: dict[str, Path]      # repo → path
    finished_at: datetime | None
    blocked_reason: str | None
    created_at: datetime
    spec_count: int
    log_path: Path | None
    orphan: bool                    # any worktree path missing on disk
    tests_failing: bool             # any test_results entry != "pass"

def build_task_index(state, workspace_root: Path) -> list[TaskSummary]:
    """Active tasks first, then finished-awaiting-close; each sorted by created_at desc."""

@dataclass(frozen=True)
class SpecEntry:
    task_slug: str | None           # None = main checkout (legacy)
    path: Path
    mtime: float
    title: str                      # first '# ' heading, or filename stem

def find_all_specs(state, workspace_root: Path) -> list[SpecEntry]:
    """For each task, glob every worktree's docs/superpowers/specs/*.md.
    Also glob workspace_root/docs/superpowers/specs/*.md (legacy, task_slug=None).
    Sorted: grouped by task (active first), newest mtime first within group."""
```

`find_spec(name_or_path, task: str | None = None)` in `spec_discovery.py` gains the `task` kwarg. When set, it searches only that task's worktrees. When `name_or_path is None and task is not None`, returns the newest spec in that task.

### Section 2 — View UX

All four subcommands accept `--task <slug>`. With no args, each opens a picker.

**Shared widget: `TaskPicker`** (new, `mship/cli/view/_picker.py`)
Textual `DataTable` with columns `slug · phase · repos · age · flags`. Flags: `⚠ close` (finished, awaiting close), `🚫 blocked`, `🧪 fail` (tests failing), `⚠ orphan` (worktree missing). Keybindings: `j/k` or `↑/↓` to move, `Enter` to drill (where applicable — `spec`, `logs`, `diff`), `Esc` to return from a drill-down to the picker, `q` to quit. Watch mode refreshes rows in place without losing cursor position (preserve by slug, not row index).

**`view status`**
Default: render the existing single-task status block for each task in `build_task_index`, stacked vertically inside a `VerticalScroll`. No drill-down; the stacked view is the God view. `--task <slug>`: the existing single-task render.

**`view spec`**
Default: render a spec-index picker (columns `task · filename · modified · title`) from `find_all_specs`. Enter swaps the pane to the existing `SpecView` rendering that spec; `Esc` returns to the index. `--task <slug>`: newest spec in that task's worktrees. Explicit `name_or_path` unchanged (scans all worktrees when not qualified by `--task`). `--task` and an explicit `name_or_path` are mutually exclusive — erroring early.

**`view logs`**
Default: `TaskPicker`. Enter opens the existing logs tail for the selected task. `--task <slug>`: skip picker.

**`view diff`**
Default: `TaskPicker` with an extra `files changed` column. Enter opens the existing per-worktree diff browser. `--task <slug>`: skip picker.

### Section 3 — Edge cases, testing, migration

**Edge cases**
- **No tasks in state** — picker shows empty state: *"No tasks. `mship spawn "…"` to start one."*
- **Worktree path missing on disk** — `orphan=True`; row shows `⚠ orphan` flag; Enter is a no-op with a toast pointing to `mship prune`.
- **Legacy main-checkout specs** — appear in spec index with `task: —`.
- **Same spec filename in multiple worktrees** — each is its own row; `task` column disambiguates.
- **Spec glob scope** — `docs/superpowers/specs/*.md` (one level, not recursive) to stay cheap.
- **`--task <slug>` with unknown slug** — exit code 1, listing available slugs (mirrors `skill install` error style).
- **`view spec --task X name.md`** — rejected as mutually exclusive.

**Testing**
- Unit: `build_task_index` and `find_all_specs` against fake state + `tmp_path` filesystems. Cases: multi-task, multi-worktree, orphan, legacy-main-checkout spec, task with zero specs, finished-awaiting-close ordering.
- TUI: reuse the existing `rendered_text()` pattern — assert picker rows, flags, drill-down swap, `--task` narrows.
- CLI: Typer runner — `--task unknown` exits 1 with slug list, no-args default opens picker, `--task` + explicit spec name rejected.

**Migration / compatibility**
- No state-model changes.
- Flag names unchanged; `--task` is additive.
- Zellij layouts pointing at `mship view status` now see the stacked God view instead of "No active task" when no task is current — net improvement.
- `find_spec` call sites that pass a single positional `name_or_path` still work (new `task` kwarg is keyword-only, default None preserves current behavior).

## Risk / Open Questions

- **Textual picker focus management during watch refresh** — need to preserve selected row across refreshes. Approach: cache by task-slug, not by row index. Verify in a TUI test.
- **Spec title extraction** — grep first `# ` heading; if none, fall back to filename stem. Keep the scan bounded (read only first ~2KB of each spec).

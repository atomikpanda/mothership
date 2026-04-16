# UX polish: diff N/M/D/R indicators, review-tab journal, spec fallback

**Status:** design, approved
**Date:** 2026-04-16
**Task slug:** `ux-polish-diff-nmd-indicators-review-tab-journal-spec-fallback`
**Branch:** `feat/ux-polish-diff-nmd-indicators-review-tab-journal-spec-fallback`

## Summary

Three independent UX wins for the `mship view` / zellij layout surface:

1. **Diff tree status indicators** — prefix each file in the `mship view diff` tree with a colored status letter (N/M/D/R) so reviewers can triage at a glance.
2. **Review-tab journal pane** — replace the plain Shell pane on the Review zellij tab with a split (Shell top, Journal bottom) so reviewers see live journal context alongside the diff.
3. **Spec-view task fallback** — when `mship view spec` would otherwise show "Spec not found" for the default (no-name) lookup on a task that has no spec yet, render a synthetic markdown page with the task description + recent journal entries instead.

Out of scope for this spec: state-change alerts (#5 in the UX review). Deferred — requires Textual `notify()` research and is medium effort.

## Motivation

- **Diff tree (#3):** current leaf labels are `path  +A -D`. You can't tell at a glance which files are new vs. deleted vs. modified — you have to click each one. One-letter prefix is the cheapest, highest-leverage improvement to the review surface.
- **Review tab (#4):** the Review zellij tab is the "look at the diff and decide if it's done" pane. Reviewers lose context on why changes were made without journal visibility. The Dev and Run tabs already have a Journal pane; Review is the obvious missing case.
- **Spec fallback (#6):** early in a task there is no spec yet, but `mship view spec --watch` (wired into the Plan tab) dies with "Spec not found." The pane is then useless during exactly the moment the spec is being authored. Showing task description + journal gives the pane immediate value and self-documents what the task is about.

## Design

### 1. Diff tree N/M/D/R indicators

#### Data: `FileDiff.status` and `old_path`

`src/mship/core/view/diff_sources.py` — extend `FileDiff`:

```python
@dataclass(frozen=True)
class FileDiff:
    path: str
    additions: int
    deletions: int
    body: str
    status: str              # "N" | "M" | "D" | "R"
    old_path: str | None = None   # set only when status == "R"

    @property
    def is_lockfile(self) -> bool:
        return Path(self.path).name in _LOCKFILE_NAMES
```

Detect in `_parse_one_chunk`:

- scan the chunk's header lines for `new file mode` → `status = "N"`
- for `deleted file mode` → `status = "D"`
- for `rename from <old>` + `rename to <new>` → `status = "R"`, `path = <new>`, `old_path = <old>`
- otherwise → `status = "M"`

Detection runs on the header (lines before the first `@@`), so it's O(lines in header), trivial. When both `new file mode` and content lines are present we still pick N. Rename detection must handle `rename from`/`rename to` which appear instead of `+++ b/`/`--- a/` paths; current path extraction will fail for renames without the new rule.

#### Merge semantics

`_merge_file_diffs` combines per-file committed + uncommitted diffs. When both sides have the same path, the merged `FileDiff` takes the **committed-side status** (so a file that was newly added in a commit and then further edited uncommitted remains `N` overall — the review lens is "what's new on this branch vs. base"). Additions and deletions sum; bodies concatenate with the existing `-- uncommitted --` separator.

For renames (`R`) — if the committed side is `R` and the uncommitted side is `M`, the merged row stays `R` with the new path.

#### Rendering

`src/mship/cli/view/diff.py` — replace the string label construction in `_rebuild_tree` (currently at lines ~143-145):

```python
suffix = "(binary)" if "new binary file" in f.body else f"+{f.additions} -{f.deletions}"
display_path = f"{f.path} ← {f.old_path}" if f.status == "R" and f.old_path else f.path
label = Text.assemble(
    (f.status, _STATUS_STYLES[f.status]),
    "  ",
    display_path,
    "  ",
    suffix,
)
node.add_leaf(label, data=("file", p, f.path))
```

Where `_STATUS_STYLES` is module-level:

```python
_STATUS_STYLES = {
    "N": "green",
    "M": "yellow",
    "D": "red",
    "R": "blue",
}
```

Textual `Tree.add_leaf` accepts a Rich `Text` for `label`; colors render in terminal and degrade cleanly under no-color environments.

### 2. Review zellij tab — Journal pane

`src/mship/cli/layout.py` — the Review tab KDL block changes from:

```kdl
tab name="Review" {
    pane split_direction="vertical" {
        pane size="70%" name="Diff" command="mship" close_on_exit=false { args "view" "diff" "--watch"; }
        pane size="30%" name="Shell"
    }
}
```

to:

```kdl
tab name="Review" {
    pane split_direction="vertical" {
        pane size="70%" name="Diff" command="mship" close_on_exit=false { args "view" "diff" "--watch"; }
        pane size="30%" split_direction="horizontal" {
            pane name="Shell"
            pane name="Journal" command="mship" close_on_exit=false { args "view" "journal" "--watch"; }
        }
    }
}
```

Right column is split 50/50 Shell/Journal. Proportions are the zellij default when no `size=` is given on children; if we want explicit values we can set `size="50%"` on each, but leaving them implicit keeps the KDL shorter and matches the existing Dev/Run tabs.

### 3. Spec-view fallback for tasks with no spec

`src/mship/cli/view/spec.py` — `SpecView._refresh_content` currently catches `SpecNotFoundError` and renders a plain error string. Add a branch:

```python
except SpecNotFoundError as e:
    if self._name_or_path is None:
        body = self._render_task_fallback(default_error=str(e))
        self._last_source = body
        self._last_error = ""
        self._markdown.update(body)
        self._error_static.update("")
    else:
        error_msg = f"Spec not found: {e}"
        self._last_source = ""
        self._last_error = error_msg
        self._markdown.update("")
        self._error_static.update(error_msg)
```

`_render_task_fallback(default_error)` returns a markdown string:

1. Resolve the active task slug: `self._task_filter or (self._state.current_task if self._state else None)`.
2. If no slug, return the default error wrapped in a minimal markdown frame — we have nothing better to show.
3. Else load the task from `self._state.tasks[slug]` and read recent journal entries via `self._log_manager.read(slug)` (latest 10, newest first).
4. Assemble markdown:

```markdown
# No spec yet for task `<slug>`

**Phase:** `<phase>`  ·  **Branch:** `<branch>`

## Task description
<task.description>

## Recent journal
- **<YYYY-MM-DD HH:MM:SS>** — <message>
- …

_Write a spec with your preferred flow and save it to `docs/superpowers/specs/`._
```

If the task has no journal entries, show `_No journal entries yet._`.

#### Dependency injection

Constructor adds a `log_manager` parameter (default `None`, so tests can pass a stub):

```python
def __init__(
    self,
    workspace_root: Path,
    name_or_path: Optional[str],
    *,
    task: Optional[str] = None,
    state=None,
    log_manager=None,
    **kw,
):
```

The CLI `register` wires `container.log_manager()` through in both the direct-render path and the picker path.

### Testing strategy

- `tests/view/test_diff_sources.py` — 4 new tests for status detection (N/M/D/R) and 1 for merge semantics keeping committed status precedence. Existing tests should continue to pass; `FileDiff` gains fields with defaults (`status="M"`, `old_path=None`).
- `tests/view/test_view_diff.py` — 1 Textual smoke test: feed a `_test_override` with all four statuses, assert each appears in `tree_labels()`.
- `tests/view/test_view_spec.py` — 2 tests:
  - Fallback renders when `name_or_path is None`, state has a current task, and no spec exists anywhere.
  - Explicit name still errors with "Spec not found" and does NOT render fallback.
- `tests/cli/test_layout.py` (or wherever layout is currently tested) — assert the Review tab's template contains both a Shell pane and a Journal pane with the `mship view journal --watch` args.

### Migration / backward compatibility

- `FileDiff` gets two new fields with defaults, so existing constructors in tests keep working.
- `SpecView.__init__` gets one new keyword with default `None`; existing callers outside the CLI (tests) can pass `log_manager=None` and get the old behavior (error without fallback when no slug is resolvable).
- The layout file is regenerated via `mship layout init --force`; users who already have `~/.config/zellij/layouts/mothership.kdl` will need `--force` to see the new Review layout. The README already documents this.

## Out of scope

- State-change alerts (Textual `notify()` / bell integration) — deferred.
- Rename detection quality improvements (similarity thresholds, cross-file renames) — git reports renames per diff; we trust its output.
- Making the fallback clickable / offering a "start spec" action — one pass through the scroll view is enough for v1.
- Customizing Shell/Journal split ratio on the Review tab via config.

## Open questions

None at design-approval time.

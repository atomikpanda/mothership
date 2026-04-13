# `mship view diff` — File-List + Diff Panes

**Status:** Design approved, ready for implementation plan.
**Date:** 2026-04-13

## Purpose

The v1 `mship view diff` dumps every changed file's diff into one scroll buffer. When a lockfile or generated file changes, the buffer becomes thousands of lines and obscures everything else. This refactor turns the view into a two-pane browser — file tree on the left, diff for the selected file on the right — with automatic collapse for noisy lockfiles.

## Non-Goals

- Editing, staging, or committing. View remains strictly read-only.
- Configurable lockfile list for v1. A built-in frozenset covers the common cases; extending it is a one-line code change.
- Side-by-side word diff, hunk expansion, or inline review comments. Post-v1.
- Persistence of UI state (expanded/collapsed, selected file) across invocations.

## Architecture

Replace `DiffView`'s single-widget body with a `Horizontal` split. Left child is a Textual `Tree` widget (30% width) that lists worktrees and files; right child is a scrollable diff pane (70% width) showing the selected file's diff. Both panes live inside the existing `ViewApp` base, so `--watch`, alt-screen, no-yank scroll preservation, and `q/r` keys keep working.

Business logic stays in `core/view/diff_sources.py`: the current `collect_worktree_diff` returns a single combined blob; this change splits that blob into per-file chunks via a new `split_diff_by_file` helper and returns a list of `FileDiff` records. The view consumes the list directly.

### Files touched

- **Modify** `src/mship/core/view/diff_sources.py` — add `FileDiff` dataclass, `split_diff_by_file`, `_LOCKFILE_NAMES` set; change `WorktreeDiff.combined: str` to `WorktreeDiff.files: tuple[FileDiff, ...]` plus a `.combined` computed property for backward-compat callers.
- **Modify** `src/mship/cli/view/diff.py` — rewrite `DiffView` around the two-pane layout, selection state, lockfile expand-on-demand.
- **Modify** `tests/core/view/test_diff_sources.py` — extend for `split_diff_by_file`, `FileDiff.is_lockfile`.
- **Modify** `tests/cli/view/test_diff_view.py` — new tests for tree population, auto-collapse, selection-change, lockfile toggle.

## Data Model

### `FileDiff`

```python
@dataclass(frozen=True)
class FileDiff:
    path: str              # "src/foo.py" or "pnpm-lock.yaml"
    additions: int
    deletions: int
    body: str              # the diff --git ... hunk for this one file, trailing newline

    @property
    def is_lockfile(self) -> bool:
        return Path(self.path).name in _LOCKFILE_NAMES
```

### `WorktreeDiff` (revised)

```python
@dataclass(frozen=True)
class WorktreeDiff:
    root: Path
    files: tuple[FileDiff, ...]

    @property
    def files_changed(self) -> int:
        return len(self.files)

    @property
    def combined(self) -> str:
        """Legacy accessor — concatenation of every file's body."""
        return "".join(f.body for f in self.files)
```

Existing callers that read `.combined` keep working. Existing tests that assert on `.combined` keep passing. New callers read `.files`.

### `_LOCKFILE_NAMES`

Module-level frozenset in `diff_sources.py`:

```
package-lock.json, pnpm-lock.yaml, yarn.lock,
poetry.lock, uv.lock, Pipfile.lock,
Cargo.lock, Gemfile.lock, composer.lock, go.sum
```

Matched against `Path(file.path).name` (basename). Extending the list is a code change, not a config change, in v1.

### `split_diff_by_file(combined: str) -> list[FileDiff]`

Parses a concatenated `diff --git` stream. Splits on lines beginning with `diff --git `. For each chunk:

- `path` — extracted from the `+++ b/<path>` line. For pure deletions (`+++ /dev/null`), fall back to `--- a/<path>`. For binary stubs (`new binary file, N bytes`), use the path from the header `diff --git a/<p> b/<p>`.
- `additions` — count of lines beginning with `+` but not `+++`.
- `deletions` — count of lines beginning with `-` but not `---`.
- `body` — the entire chunk verbatim (header + hunks + trailing newline if present).

Pure property: given a concatenated diff, the sum of `split_diff_by_file(x).body` concatenated is equal to `x` (modulo a normalized trailing newline).

## Layout & Behavior

### Split

Textual `Horizontal` inside the body. Left child 30% minimum width (clamped to 24 cells minimum), right child fills the rest.

### File tree (left)

Textual `Tree[FileDiff | WorktreeDiff | None]` with a hidden root.

- Children of root: one node per **non-clean** worktree, label `▶ <repo or path> · <N> files · +<A> -<D>`, where A/D sum across files. Clean worktrees are omitted from the tree.
- Children of each worktree: one leaf per file, label `<path>  +<A> -<D>`. Binary stub files show `<path>  (binary)`.
- **Auto-collapse:** a worktree with more than 20 files starts collapsed on mount. ≤20 → expanded.

### Diff pane (right)

- When the selection is a file node → render that file's `body` through the existing delta-or-rich rendering path, wrapped in `Text.from_ansi`.
- When the selection is a worktree node → show one summary line: `<path>: <N> files changed, +<A> -<D>`.
- When the tree is empty → show "No changes." centered.
- **Lockfile collapse:** if the selected file's `is_lockfile` is true and the session-expand set does not contain the `(worktree_path, file_path)` tuple, render:
  ```
  <file>: +<A> -<D> (collapsed — press e to expand)
  ```
  Pressing `e` adds the tuple to the expand set and re-renders. Pressing `e` again removes it. Non-lockfile selections: `e` is a no-op.

### Keys

- `j/k`, `↑/↓` — move tree cursor.
- `h`/`l` — focus the tree / the diff pane (for scrolling within the diff).
- `Enter` — on a worktree node, toggle expand/collapse; on a file node, focus the diff pane.
- `z` — toggle expand/collapse on the worktree containing the current selection (or on the selection itself if it is a worktree node).
- `e` — toggle lockfile expansion for the selected file; no-op otherwise.
- Base class keys (`q`, `r`, `PgUp/PgDn`, `Home/End`) keep working; `PgUp/PgDn` and `Home/End` act on whichever pane has focus.

### Selection state

- On initial mount (and on every `--watch` refresh that produces a non-empty tree), select the first file node in iteration order. If the previously selected `(worktree, file)` tuple is still present after a refresh, preserve it instead.
- On selection change: diff pane scroll resets to `y=0` (not the no-yank preservation — new content, new context).
- Lockfile expansion state is per-session; it survives refreshes within the same `DiffView` instance and is discarded on exit.

## Error Handling

- Worktree path disappeared between `--watch` refreshes → the worktree node is silently removed on the next refresh; if it was the selected worktree, fall back to the first available file.
- `collect_worktree_diff` raises for a specific worktree → render a single tree node `▶ <path> (error: <message>)` with no children; selecting it shows the error in the diff pane. Audit surfaces these through the existing `_refresh_content` `except Exception` catch.
- Empty tree on mount → show "No changes." in the diff pane. `e`/`z` are no-ops.
- `split_diff_by_file` on malformed input (missing `+++ b/...` line) → synthesize a `FileDiff(path="<unknown>", body=chunk, additions=0, deletions=0)` rather than raising. A malformed chunk shouldn't crash the view.

## Testing

### Unit tests in `tests/core/view/test_diff_sources.py`

- `split_diff_by_file` with one modified file, two modified files, one synthesized untracked file, a binary stub. For each: asserts path, additions, deletions, and that `"".join(f.body for f in result) == combined` within a trailing-newline tolerance.
- `split_diff_by_file` on the empty string returns `[]`.
- `FileDiff.is_lockfile` is True for every name in `_LOCKFILE_NAMES` and False for `src/foo.py`, `README.md`, `Taskfile.yml`.
- `collect_worktree_diff` returns a `WorktreeDiff` whose `.files` is a non-empty tuple of `FileDiff` for a repo with modified + untracked changes; `.combined` still reads back the full concatenation (backward-compat).

### TUI tests in `tests/cli/view/test_diff_view.py`

- Mount with two worktrees, three files each (mocked via a test-only injection point on `DiffView` that skips the real `collect_worktree_diff` call) → tree has two top-level nodes, each with three children, first file selected, diff pane shows its body.
- Worktree with 25 files starts collapsed (`node.is_expanded is False`); worktree with 3 files starts expanded.
- Lockfile placeholder: seed a file named `pnpm-lock.yaml` with a large body; assert the diff pane shows the collapsed placeholder text and does **not** include a line from the body. Press `e`; assert a body line now appears. Press `e` again; back to placeholder.
- Selection-change scroll reset: seed a file long enough to scroll; focus the diff pane; scroll down; move tree cursor to a different file; assert diff-pane `scroll_y == 0`.
- Refresh preserves selection when possible: start with file `A`, simulate a refresh where `A` still exists → `A` stays selected; simulate a refresh where `A` no longer exists → first available file becomes selected.

## Migration Notes

No config changes. No CLI flag changes. `mship view diff` still renders `delta` output when available and falls back to rich otherwise. `WorktreeDiff.combined` is preserved as a computed property so any downstream consumer (none currently outside the view) keeps working.

## Out of Scope (post-v1)

- Configurable lockfile list in `mothership.yaml`.
- Word/character-level highlighting (e.g. `delta --side-by-side`).
- File-tree search (`/path`).
- Hunk-level collapse within a large non-lockfile.
- Persisted UI state across invocations.

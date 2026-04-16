# UX polish: diff N/M/D/R, review-tab journal, spec fallback — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship three independent UX wins: colored N/M/D/R status letters on the `mship view diff` tree, a Journal pane on the Review zellij tab, and a task-description/journal fallback for `mship view spec` when no spec exists yet.

**Architecture:** Each feature lands in its own commit on the task branch. TDD: tests first, see them fail, implement, see them pass, commit. No cross-feature coupling — any task can be reverted independently.

**Tech Stack:** Python 3.12, Textual (TUI), Rich (text styling), typer (CLI), pytest + pytest-asyncio, Typer CliRunner. Spec: `docs/superpowers/specs/2026-04-16-ux-polish-diff-review-tab-spec-fallback-design.md`.

---

## Working directory

All work happens in the pre-existing task worktree:
`/home/bailey/development/repos/mothership/.worktrees/feat/ux-polish-diff-nmd-indicators-review-tab-journal-spec-fallback`
Branch: `feat/ux-polish-diff-nmd-indicators-review-tab-journal-spec-fallback`

**Before any edit or commit:** `cd` into the worktree. `git branch` must show `feat/ux-polish-…` — if it shows `main`, stop and relocate.

Run tests from inside the worktree: `uv run pytest tests/...` (the project uses uv; `pyproject.toml` defines the test runner).

---

## File structure

**Feature 1 — diff status indicators:**
- `src/mship/core/view/diff_sources.py` — add `status`, `old_path` to `FileDiff`; detect in `_parse_one_chunk`; keep committed-side status in `_merge_file_diffs`.
- `src/mship/cli/view/diff.py` — add `_STATUS_STYLES`; construct label as a Rich `Text` with colored status letter in `_rebuild_tree`.
- `tests/core/view/test_diff_sources.py` — status detection (N/M/D/R) + merge rule.
- `tests/cli/view/test_diff_view.py` — tree label carries status letter.

**Feature 2 — review tab journal:**
- `src/mship/cli/layout.py` — `_TEMPLATE`, Review tab block.
- `tests/cli/test_layout.py` — Review tab contains Shell and Journal.

**Feature 3 — spec view fallback:**
- `src/mship/cli/view/spec.py` — `SpecView.__init__` accepts `log_manager`; `_refresh_content` branches on `SpecNotFoundError`; new `_render_task_fallback`. `register` wires `container.log_manager()`.
- `tests/cli/view/test_spec_view.py` — fallback renders; explicit-name miss still errors.

---

## Task 1: Add `status` and `old_path` to `FileDiff`, detect in `_parse_one_chunk`

**Files:**
- Modify: `src/mship/core/view/diff_sources.py` (FileDiff dataclass + `_parse_one_chunk`)
- Test: `tests/core/view/test_diff_sources.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/core/view/test_diff_sources.py`:

```python
def test_status_new_file():
    chunk = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+print('hi')\n"
    )
    (f,) = split_diff_by_file(chunk)
    assert f.status == "N"
    assert f.old_path is None
    assert f.path == "new.py"


def test_status_deleted_file():
    chunk = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-print('bye')\n"
    )
    (f,) = split_diff_by_file(chunk)
    assert f.status == "D"
    assert f.old_path is None
    assert f.path == "gone.py"


def test_status_modified_file():
    chunk = (
        "diff --git a/mod.py b/mod.py\n"
        "index abc..def 100644\n"
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1,1 +1,2 @@\n"
        " keep\n"
        "+add\n"
    )
    (f,) = split_diff_by_file(chunk)
    assert f.status == "M"
    assert f.old_path is None


def test_status_rename_without_changes():
    chunk = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 100%\n"
        "rename from old.py\n"
        "rename to new.py\n"
    )
    (f,) = split_diff_by_file(chunk)
    assert f.status == "R"
    assert f.path == "new.py"
    assert f.old_path == "old.py"


def test_status_rename_with_changes():
    chunk = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 90%\n"
        "rename from old.py\n"
        "rename to new.py\n"
        "--- a/old.py\n"
        "+++ b/new.py\n"
        "@@ -1,1 +1,2 @@\n"
        " keep\n"
        "+add\n"
    )
    (f,) = split_diff_by_file(chunk)
    assert f.status == "R"
    assert f.path == "new.py"
    assert f.old_path == "old.py"
    assert f.additions == 1
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:
```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/ux-polish-diff-nmd-indicators-review-tab-journal-spec-fallback
uv run pytest tests/core/view/test_diff_sources.py::test_status_new_file tests/core/view/test_diff_sources.py::test_status_deleted_file tests/core/view/test_diff_sources.py::test_status_modified_file tests/core/view/test_diff_sources.py::test_status_rename_without_changes tests/core/view/test_diff_sources.py::test_status_rename_with_changes -v
```

Expected: 5 failures — `AttributeError: 'FileDiff' object has no attribute 'status'` on every test.

- [ ] **Step 3: Update `FileDiff` dataclass with status + old_path fields**

Edit `src/mship/core/view/diff_sources.py` — find the existing `FileDiff` dataclass (lines 21-30) and replace with:

```python
@dataclass(frozen=True)
class FileDiff:
    path: str
    additions: int
    deletions: int
    body: str
    status: str = "M"  # "N" | "M" | "D" | "R"
    old_path: str | None = None  # set only when status == "R"

    @property
    def is_lockfile(self) -> bool:
        return Path(self.path).name in _LOCKFILE_NAMES
```

Defaults keep every existing callsite (tests, `_merge_file_diffs`) working unchanged — they'll all get `status="M"` until we opt in.

- [ ] **Step 4: Add regex + helper for status detection**

Edit `src/mship/core/view/diff_sources.py`. Immediately above `_parse_one_chunk` (around line 91), add:

```python
_RENAME_FROM = re.compile(r"^rename from (.+)$", re.MULTILINE)
_RENAME_TO = re.compile(r"^rename to (.+)$", re.MULTILINE)


def _detect_status(header: str) -> tuple[str, str | None]:
    """Return (status, old_path) from a diff chunk's header region.

    `header` is the slice of the chunk before the first `@@` line (or the
    whole chunk when there are no hunks, e.g. pure renames)."""
    m_from = _RENAME_FROM.search(header)
    m_to = _RENAME_TO.search(header)
    if m_from and m_to:
        return "R", m_from.group(1)
    if "\nnew file mode " in "\n" + header or header.startswith("new file mode "):
        return "N", None
    if "\ndeleted file mode " in "\n" + header or header.startswith("deleted file mode "):
        return "D", None
    return "M", None
```

- [ ] **Step 5: Wire status detection into `_parse_one_chunk`**

Edit `_parse_one_chunk` in `src/mship/core/view/diff_sources.py`. Current body (line 91-112):

```python
def _parse_one_chunk(chunk: str) -> FileDiff:
    m = _PATH_FROM_PLUS.search(chunk)
    if m and m.group(1) != "/dev/null":
        path = m.group(1)
    else:
        m = _PATH_FROM_MINUS.search(chunk)
        if m and m.group(1) != "/dev/null":
            path = m.group(1)
        else:
            m = _PATH_FROM_HEADER.search(chunk)
            path = m.group(1) if m else "<unknown>"

    additions = 0
    deletions = 0
    for line in chunk.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return FileDiff(path=path, additions=additions, deletions=deletions, body=chunk)
```

Replace with:

```python
def _parse_one_chunk(chunk: str) -> FileDiff:
    # Header region: everything up to the first `@@` hunk (if any).
    hunk_idx = chunk.find("\n@@")
    header = chunk if hunk_idx == -1 else chunk[:hunk_idx]

    status, old_path = _detect_status(header)

    if status == "R":
        # Path is the rename target; extracted from `rename to <new>`.
        m = _RENAME_TO.search(header)
        path = m.group(1) if m else "<unknown>"
    else:
        m = _PATH_FROM_PLUS.search(chunk)
        if m and m.group(1) != "/dev/null":
            path = m.group(1)
        else:
            m = _PATH_FROM_MINUS.search(chunk)
            if m and m.group(1) != "/dev/null":
                path = m.group(1)
            else:
                m = _PATH_FROM_HEADER.search(chunk)
                path = m.group(1) if m else "<unknown>"

    additions = 0
    deletions = 0
    for line in chunk.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return FileDiff(
        path=path,
        additions=additions,
        deletions=deletions,
        body=chunk,
        status=status,
        old_path=old_path,
    )
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run:
```bash
uv run pytest tests/core/view/test_diff_sources.py -v
```

Expected: all tests pass, including the pre-existing ones (which didn't assert on status).

- [ ] **Step 7: Run the whole test suite to catch regressions**

Run:
```bash
uv run pytest -q
```

Expected: green, or only failures unrelated to our changes. If any test in `tests/cli/view/test_diff_view.py` fails because it constructed `FileDiff` without `status`, the default `"M"` should absorb that — but if you see a failure, investigate before proceeding.

- [ ] **Step 8: Commit**

```bash
git add src/mship/core/view/diff_sources.py tests/core/view/test_diff_sources.py
git commit -m "feat(view): detect file status (N/M/D/R) in diff parser

Add FileDiff.status and FileDiff.old_path. Detect new/deleted/renamed
files from diff chunk headers; default to 'M'. Prepares the diff view
for colored status prefixes per file."
```

---

## Task 2: Keep committed-side status in `_merge_file_diffs`

**Files:**
- Modify: `src/mship/core/view/diff_sources.py:_merge_file_diffs`
- Test: `tests/core/view/test_diff_sources.py`

- [ ] **Step 1: Write failing test**

Append to `tests/core/view/test_diff_sources.py`:

```python
def test_merge_keeps_committed_side_status():
    """A file newly added in a commit and further edited uncommitted stays N."""
    from mship.core.view.diff_sources import _merge_file_diffs

    committed = [FileDiff(
        path="new.py", additions=5, deletions=0,
        body="diff --git a/new.py b/new.py\nnew file mode 100644\n",
        status="N", old_path=None,
    )]
    uncommitted = [FileDiff(
        path="new.py", additions=2, deletions=1,
        body="diff --git a/new.py b/new.py\n--- a/new.py\n+++ b/new.py\n",
        status="M", old_path=None,
    )]
    merged = _merge_file_diffs(committed, uncommitted)
    (f,) = merged
    assert f.status == "N"
    assert f.additions == 7
    assert f.deletions == 1


def test_merge_uncommitted_only_keeps_its_status():
    from mship.core.view.diff_sources import _merge_file_diffs

    merged = _merge_file_diffs([], [FileDiff(
        path="only.py", additions=1, deletions=0, body="...",
        status="N", old_path=None,
    )])
    (f,) = merged
    assert f.status == "N"


def test_merge_rename_preserved():
    from mship.core.view.diff_sources import _merge_file_diffs

    committed = [FileDiff(
        path="new.py", additions=0, deletions=0,
        body="rename from old.py\nrename to new.py\n",
        status="R", old_path="old.py",
    )]
    uncommitted = [FileDiff(
        path="new.py", additions=1, deletions=0,
        body="--- a/new.py\n+++ b/new.py\n+tweak\n",
        status="M", old_path=None,
    )]
    merged = _merge_file_diffs(committed, uncommitted)
    (f,) = merged
    assert f.status == "R"
    assert f.old_path == "old.py"
```

- [ ] **Step 2: Run to verify failure**

Run:
```bash
uv run pytest tests/core/view/test_diff_sources.py::test_merge_keeps_committed_side_status tests/core/view/test_diff_sources.py::test_merge_rename_preserved -v
```

Expected: both fail. The current `_merge_file_diffs` constructs a new `FileDiff` with the defaulted `status="M"` regardless of the incoming committed status.

- [ ] **Step 3: Update `_merge_file_diffs` to preserve committed status**

Edit `src/mship/core/view/diff_sources.py`. Current body (lines 154-172):

```python
def _merge_file_diffs(
    committed: list[FileDiff], uncommitted: list[FileDiff]
) -> list[FileDiff]:
    """Merge per-file diffs. Files in both get additions/deletions summed and
    bodies concatenated with a `-- uncommitted --` separator."""
    by_path: dict[str, FileDiff] = {f.path: f for f in committed}
    for u in uncommitted:
        if u.path in by_path:
            c = by_path[u.path]
            body = c.body.rstrip("\n") + "\n-- uncommitted --\n" + u.body
            by_path[u.path] = FileDiff(
                path=u.path,
                additions=c.additions + u.additions,
                deletions=c.deletions + u.deletions,
                body=body,
            )
        else:
            by_path[u.path] = u
    return list(by_path.values())
```

Replace the merged-row construction so `status` / `old_path` come from the committed side:

```python
def _merge_file_diffs(
    committed: list[FileDiff], uncommitted: list[FileDiff]
) -> list[FileDiff]:
    """Merge per-file diffs. Files in both get additions/deletions summed and
    bodies concatenated with a `-- uncommitted --` separator. Status is
    inherited from the committed side when both sides exist (the review
    lens is 'what's new on this branch vs. base')."""
    by_path: dict[str, FileDiff] = {f.path: f for f in committed}
    for u in uncommitted:
        if u.path in by_path:
            c = by_path[u.path]
            body = c.body.rstrip("\n") + "\n-- uncommitted --\n" + u.body
            by_path[u.path] = FileDiff(
                path=u.path,
                additions=c.additions + u.additions,
                deletions=c.deletions + u.deletions,
                body=body,
                status=c.status,
                old_path=c.old_path,
            )
        else:
            by_path[u.path] = u
    return list(by_path.values())
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/core/view/test_diff_sources.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/view/diff_sources.py tests/core/view/test_diff_sources.py
git commit -m "feat(view): preserve committed-side status when merging diffs

When a file appears in both the committed diff and the uncommitted
diff (e.g. newly added in a commit, then further edited), the merged
row keeps the committed status (N/D/R). The review lens is 'what's
new on this branch vs. base', not 'what's changed since last commit'."
```

---

## Task 3: Render colored status letter on diff tree leaves

**Files:**
- Modify: `src/mship/cli/view/diff.py` (`_rebuild_tree`, module-level `_STATUS_STYLES`)
- Test: `tests/cli/view/test_diff_view.py`

- [ ] **Step 1: Write failing test**

Append to `tests/cli/view/test_diff_view.py` (below the existing `_fd` helper, add a version that takes status, then add the test):

```python
def _fd_s(path: str, status: str = "M", body: str = "", additions: int = 1,
          deletions: int = 0, old_path: str | None = None) -> FileDiff:
    return FileDiff(
        path=path, additions=additions, deletions=deletions, body=body,
        status=status, old_path=old_path,
    )


@pytest.mark.asyncio
async def test_tree_labels_carry_status_letters(tmp_path):
    wa = tmp_path / "a"
    view = DiffView(worktree_paths=[wa], use_delta=False, watch=False, interval=1.0)
    _seed(view, {
        wa: [
            _fd_s("added.py", status="N", body="diff --git a/added.py b/added.py\nnew file mode 100644\n"),
            _fd_s("mod.py", status="M", body="diff --git a/mod.py b/mod.py\n+++ b/mod.py\n+x\n"),
            _fd_s("del.py", status="D", body="diff --git a/del.py b/del.py\ndeleted file mode 100644\n"),
            _fd_s("new.py", status="R", old_path="old.py",
                  body="diff --git a/old.py b/new.py\nrename from old.py\nrename to new.py\n"),
        ],
    })
    async with view.run_test() as pilot:
        await pilot.pause()
        labels = view.tree_labels()
        # Each file's leaf label should start with its status letter.
        added_label = next(l for l in labels if "added.py" in l)
        assert added_label.lstrip().startswith("N"), added_label
        mod_label = next(l for l in labels if "mod.py" in l)
        assert mod_label.lstrip().startswith("M"), mod_label
        del_label = next(l for l in labels if "del.py" in l)
        assert del_label.lstrip().startswith("D"), del_label
        # Rename label displays "new.py ← old.py" with an R prefix.
        rename_label = next(l for l in labels if "new.py" in l and "old.py" in l)
        assert rename_label.lstrip().startswith("R"), rename_label
        assert "←" in rename_label, rename_label
```

- [ ] **Step 2: Run to verify failure**

Run:
```bash
uv run pytest tests/cli/view/test_diff_view.py::test_tree_labels_carry_status_letters -v
```

Expected: failure — labels currently are strings like `"added.py  +1 -0"`, no status letter.

- [ ] **Step 3: Add `_STATUS_STYLES` and update `_rebuild_tree`**

Edit `src/mship/cli/view/diff.py`. Near the top, below `_LARGE_WORKTREE_THRESHOLD` (line 19), add:

```python
_STATUS_STYLES: dict[str, str] = {
    "N": "green",
    "M": "yellow",
    "D": "red",
    "R": "blue",
}
```

Then find the leaf-label line in `_rebuild_tree` (currently line ~144):

```python
            for f in wd.files:
                suffix = "(binary)" if "new binary file" in f.body else f"+{f.additions} -{f.deletions}"
                node.add_leaf(f"{f.path}  {suffix}", data=("file", p, f.path))
```

Replace with:

```python
            for f in wd.files:
                suffix = "(binary)" if "new binary file" in f.body else f"+{f.additions} -{f.deletions}"
                display_path = (
                    f"{f.path} ← {f.old_path}"
                    if f.status == "R" and f.old_path
                    else f.path
                )
                label = Text.assemble(
                    (f.status, _STATUS_STYLES.get(f.status, "")),
                    "  ",
                    display_path,
                    "  ",
                    suffix,
                )
                node.add_leaf(label, data=("file", p, f.path))
```

`Text` is already imported at the top of the file (line 9: `from rich.text import Text`).

- [ ] **Step 4: Run the new test**

Run:
```bash
uv run pytest tests/cli/view/test_diff_view.py::test_tree_labels_carry_status_letters -v
```

Expected: pass.

- [ ] **Step 5: Run all diff-view tests to catch regressions**

Run:
```bash
uv run pytest tests/cli/view/test_diff_view.py tests/core/view/test_diff_sources.py -v
```

Expected: all pass. The existing `tree_labels()` assertions (`"a.py" in l`, `str(wa) in l`) still pass because `Text` stringifies to its content.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/view/diff.py tests/cli/view/test_diff_view.py
git commit -m "feat(view): colored N/M/D/R status prefix on diff tree leaves

Each file in the diff tree now shows a colored status letter: N=green
(new), M=yellow (modified), D=red (deleted), R=blue (renamed).
Renames display as 'new ← old'. Reviewers can triage files at a
glance without clicking through every row."
```

---

## Task 4: Review zellij tab gets a Journal pane

**Files:**
- Modify: `src/mship/cli/layout.py:_TEMPLATE` (Review tab block)
- Test: `tests/cli/test_layout.py`

- [ ] **Step 1: Write failing test**

Append to `tests/cli/test_layout.py`:

```python
def test_review_tab_has_journal_pane():
    """The Review tab must include a Shell pane and a Journal pane wired to
    `mship view journal --watch`."""
    # Find the Review tab block.
    assert 'tab name="Review"' in _TEMPLATE
    start = _TEMPLATE.index('tab name="Review"')
    # The Run tab starts with `tab name="Run"`; slice up to it.
    end = _TEMPLATE.index('tab name="Run"', start)
    review_block = _TEMPLATE[start:end]

    assert 'name="Shell"' in review_block, review_block
    assert 'name="Journal"' in review_block, review_block
    assert '"view" "journal" "--watch"' in review_block, review_block
```

- [ ] **Step 2: Run to verify failure**

Run:
```bash
uv run pytest tests/cli/test_layout.py::test_review_tab_has_journal_pane -v
```

Expected: failure — `assert 'name="Journal"' in review_block`.

- [ ] **Step 3: Update the Review tab in `_TEMPLATE`**

Edit `src/mship/cli/layout.py`. Current Review block (lines 40-45):

```kdl
    tab name="Review" {
        pane split_direction="vertical" {
            pane size="70%" name="Diff" command="mship" close_on_exit=false { args "view" "diff" "--watch"; }
            pane size="30%" name="Shell"
        }
    }
```

Replace with:

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

Keep surrounding tabs and indentation unchanged.

- [ ] **Step 4: Run the new test**

Run:
```bash
uv run pytest tests/cli/test_layout.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/layout.py tests/cli/test_layout.py
git commit -m "feat(layout): add Journal pane to Review zellij tab

Review tab's right column now splits 50/50 between Shell (top) and
Journal (bottom). Reviewers see live journal context next to the diff
without switching tabs. Users with an existing layout file need
\`mship layout init --force\` to pick up the change."
```

---

## Task 5: Spec view fallback renders task description + journal

**Files:**
- Modify: `src/mship/cli/view/spec.py` — `SpecView.__init__`, `_refresh_content`, new `_render_task_fallback`, CLI `register`
- Test: `tests/cli/view/test_spec_view.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/cli/view/test_spec_view.py`:

```python
# --- Spec fallback tests ---
from datetime import datetime, timezone
from mship.core.log import LogEntry
from mship.core.state import Task


class _StubLogManager:
    def __init__(self, entries: list[LogEntry]):
        self._entries = entries

    def read(self, slug: str, last=None):
        if last is None:
            return list(self._entries)
        return list(self._entries)[-last:]


def _stub_state_with_task(slug: str, description: str, phase: str, branch: str):
    """Build a minimal WorkspaceState-shaped stub with one task."""
    task = Task(
        slug=slug,
        description=description,
        phase=phase,
        branch=branch,
        affected_repos=[],
        worktrees={},
        base_branch="main",
        active_repo=None,
    )
    state = WorkspaceState(tasks={slug: task}, current_task=slug)
    return state


@pytest.mark.asyncio
async def test_spec_fallback_renders_task_description_when_no_spec(tmp_path: Path):
    state = _stub_state_with_task(
        slug="demo-task",
        description="Build the demo feature end-to-end.",
        phase="dev",
        branch="feat/demo",
    )
    entries = [
        LogEntry(
            timestamp=datetime(2026, 4, 16, 3, 1, 49, tzinfo=timezone.utc),
            message="Task spawned",
        ),
        LogEntry(
            timestamp=datetime(2026, 4, 16, 3, 1, 58, tzinfo=timezone.utc),
            message="Phase transition: plan -> dev",
        ),
    ]
    log_manager = _StubLogManager(entries)

    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        task="demo-task",
        state=state,
        log_manager=log_manager,
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        rendered = view.rendered_text()
        assert "Spec not found" not in rendered
        assert "demo-task" in rendered
        assert "Build the demo feature end-to-end." in rendered
        assert "Phase transition: plan -> dev" in rendered


@pytest.mark.asyncio
async def test_spec_explicit_name_still_errors_on_miss(tmp_path: Path):
    """Fallback only triggers when no name was specified; an explicit miss still errors."""
    state = _stub_state_with_task(
        slug="demo-task",
        description="desc",
        phase="dev",
        branch="feat/demo",
    )
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path="missing-spec",
        task="demo-task",
        state=state,
        log_manager=_StubLogManager([]),
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        rendered = view.rendered_text()
        assert "Spec not found" in rendered


@pytest.mark.asyncio
async def test_spec_fallback_handles_missing_log_manager(tmp_path: Path):
    """If no log_manager is wired, fallback still renders task description."""
    state = _stub_state_with_task(
        slug="demo-task",
        description="desc only",
        phase="plan",
        branch="feat/demo",
    )
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        task="demo-task",
        state=state,
        log_manager=None,
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        rendered = view.rendered_text()
        assert "desc only" in rendered
        assert "No journal entries yet" in rendered


@pytest.mark.asyncio
async def test_spec_fallback_falls_back_to_error_when_no_task_resolvable(tmp_path: Path):
    """With no name, no task filter, and no current_task, show the original error."""
    empty_state = WorkspaceState(tasks={}, current_task=None)
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        task=None,
        state=empty_state,
        log_manager=None,
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        rendered = view.rendered_text()
        assert "No specs found" in rendered or "Spec not found" in rendered
```

Note: `Task` and `WorkspaceState` imports — verify the import path with one extra import line near the top of the test file if missing:

```python
from mship.core.state import WorkspaceState
```

(already imported on line 49 of `tests/cli/view/test_spec_view.py`).

Verify the `Task` dataclass import path is correct. If `Task` is not exported from `mship.core.state`, read `src/mship/core/state.py` to find the right class name (could be `TaskState` or similar) and adjust the import + constructor call accordingly. This is the one place the plan can't be 100% fixed ahead of time — because the `Task` dataclass fields may have additional required args. If so, fill them with sensible defaults while keeping slug/description/phase/branch as shown.

- [ ] **Step 2: Run to verify the tests fail**

Run:
```bash
uv run pytest tests/cli/view/test_spec_view.py -v
```

Expected: the four new tests fail — `SpecView.__init__` doesn't accept `log_manager`; even without that, fallback logic doesn't exist. Existing tests should still pass.

- [ ] **Step 3: Extend `SpecView.__init__` to accept `log_manager`**

Edit `src/mship/cli/view/spec.py`. Current `__init__` (lines 21-44):

```python
    def __init__(
        self,
        workspace_root: Path,
        name_or_path: Optional[str],
        *,
        task: Optional[str] = None,
        state=None,
        **kw,
    ):
        # Strip SpecView-specific kwargs before passing to super
        kw.pop("workspace_root", None)
        kw.pop("name_or_path", None)
        kw.pop("task", None)
        kw.pop("state", None)
        super().__init__(**kw)
        self._workspace_root = workspace_root
        self._name_or_path = name_or_path
        self._task_filter = task
        self._state = state
        self._markdown: Markdown | None = None
        self._error_static: Static | None = None
        self._body: VerticalScroll | None = None
        self._last_source: str = ""
        self._last_error: str = ""
```

Replace with:

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
        # Strip SpecView-specific kwargs before passing to super
        kw.pop("workspace_root", None)
        kw.pop("name_or_path", None)
        kw.pop("task", None)
        kw.pop("state", None)
        kw.pop("log_manager", None)
        super().__init__(**kw)
        self._workspace_root = workspace_root
        self._name_or_path = name_or_path
        self._task_filter = task
        self._state = state
        self._log_manager = log_manager
        self._markdown: Markdown | None = None
        self._error_static: Static | None = None
        self._body: VerticalScroll | None = None
        self._last_source: str = ""
        self._last_error: str = ""
```

- [ ] **Step 4: Add `_render_task_fallback` helper**

Edit `src/mship/cli/view/spec.py`. Add this method to `SpecView`, placed right before `rendered_text` (around line 76):

```python
    def _render_task_fallback(self, default_error: str) -> str:
        """Build a markdown document for the 'no spec yet' case.

        Uses the active task slug (from `task` filter or `state.current_task`)
        to pull the task description and most recent journal entries.
        Returns `None`-equivalent fallback (just the error) when no task can
        be resolved — the caller decides what to do then."""
        slug = self._task_filter
        if slug is None and self._state is not None:
            slug = getattr(self._state, "current_task", None)
        if slug is None or self._state is None or slug not in self._state.tasks:
            return f"# {default_error}\n"

        task = self._state.tasks[slug]
        phase = getattr(task, "phase", "?")
        branch = getattr(task, "branch", "?")
        description = getattr(task, "description", "") or "_(no description)_"

        lines: list[str] = [
            f"# No spec yet for task `{slug}`",
            "",
            f"**Phase:** `{phase}`  ·  **Branch:** `{branch}`",
            "",
            "## Task description",
            description,
            "",
            "## Recent journal",
        ]

        entries = []
        if self._log_manager is not None:
            try:
                entries = self._log_manager.read(slug, last=10)
            except TypeError:
                # Older stub/log managers without `last=` kwarg.
                entries = self._log_manager.read(slug)[-10:]

        if not entries:
            lines.append("_No journal entries yet._")
        else:
            for e in entries:
                ts = e.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                lines.append(f"- **{ts}** — {e.message}")

        lines.append("")
        lines.append("_Write a spec with your preferred flow and save it to `docs/superpowers/specs/`._")
        return "\n".join(lines) + "\n"
```

- [ ] **Step 5: Wire fallback into `_refresh_content`**

Edit `_refresh_content` in `src/mship/cli/view/spec.py`. Current (lines 55-74):

```python
    def _refresh_content(self) -> None:
        assert self._markdown is not None
        assert self._error_static is not None
        assert self._body is not None
        was_at_end = self._body.scroll_y >= (self._body.max_scroll_y - 1)
        prev_y = self._body.scroll_y
        try:
            path = find_spec(self._workspace_root, self._name_or_path, task=self._task_filter, state=self._state)
            source = path.read_text()
            self._last_source = source
            self._last_error = ""
            self._markdown.update(source)
            self._error_static.update("")
        except SpecNotFoundError as e:
            error_msg = f"Spec not found: {e}"
            self._last_source = ""
            self._last_error = error_msg
            self._markdown.update("")
            self._error_static.update(error_msg)
        self.call_after_refresh(self._restore_scroll, prev_y, was_at_end)
```

Replace the `except SpecNotFoundError` branch:

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

- [ ] **Step 6: Wire `log_manager` through the CLI `register` function**

Edit `register` in `src/mship/cli/view/spec.py`. There are two places `SpecView(...)` is constructed (lines ~179 and ~192). In both, add `log_manager=container.log_manager()`.

First call site (around line 179):

```python
            view = SpecView(
                workspace_root=workspace_root,
                name_or_path=name_or_path,
                task=task,
                state=state,
                log_manager=container.log_manager(),
                watch=watch,
                interval=interval,
            )
```

The picker flow's `SpecIndexApp` (line 192) is a different class — it doesn't need log_manager. Leave it.

- [ ] **Step 7: Run the fallback tests**

Run:
```bash
uv run pytest tests/cli/view/test_spec_view.py -v
```

Expected: pass.

If `WorkspaceState`/`Task` import errors occur, open `src/mship/core/state.py`, find the correct names + required fields, and update the stub helper in the tests. Then re-run.

- [ ] **Step 8: Run full test suite for regressions**

Run:
```bash
uv run pytest -q
```

Expected: green. If anything in `tests/cli/view/test_spec_view.py` that was previously passing (e.g. `test_spec_view_missing_spec`) breaks, it's likely because that test used `name_or_path="nope"` — an explicit miss — which must still show "Spec not found". Our branch preserves that path.

- [ ] **Step 9: Commit**

```bash
git add src/mship/cli/view/spec.py tests/cli/view/test_spec_view.py
git commit -m "feat(view): spec view falls back to task context when no spec exists

When \`mship view spec\` can't find a spec and no explicit name was
given, render a synthetic markdown page with the task slug, phase,
branch, description, and recent journal entries. Explicit-name
misses still show the original 'Spec not found' error. Makes the
Plan-tab spec pane useful during the moments a spec is first being
authored."
```

---

## Task 6: End-to-end verification

- [ ] **Step 1: Run the full test suite**

Run:
```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/ux-polish-diff-nmd-indicators-review-tab-journal-spec-fallback
uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Smoke-test the diff view manually**

Make a tiny change in the worktree: add a trivial new file, rename any existing unimportant file, delete any unimportant file. Then run:

```bash
uv run mship view diff
```

Expected: the tree shows green `N`, blue `R` (with `new ← old`), red `D`, and yellow `M` prefixes. Press `q` to quit when verified.

Revert the test changes:
```bash
git checkout .
git clean -f
```

- [ ] **Step 3: Smoke-test the spec fallback**

Run:
```bash
uv run mship view spec
```

Expected: since this task has no spec under its own name (only the design doc with a date-prefixed name), the fallback OR the newest spec renders — either is acceptable as long as no "Spec not found" error crashes the view. Press `q` to quit.

If you want to force the fallback path, rename the `docs/superpowers/specs` dir in the worktree temporarily, run, then restore:
```bash
mv docs/superpowers/specs docs/superpowers/_specs_hidden
uv run mship view spec   # expect fallback markdown showing task/journal
mv docs/superpowers/_specs_hidden docs/superpowers/specs
```

- [ ] **Step 4: Regenerate and inspect the zellij layout**

Run:
```bash
uv run mship layout init --force
cat ~/.config/zellij/layouts/mothership.kdl | sed -n '/tab name="Review"/,/tab name="Run"/p'
```

Expected: the printed Review block contains both `name="Shell"` and `name="Journal"` with `args "view" "journal" "--watch"`.

- [ ] **Step 5: Log progress and transition to review**

```bash
mship journal "Implemented 3 UX polish features (diff N/M/D/R, review journal, spec fallback). All tests green. Ready for review." --action "completed implementation" --test-state pass
mship phase review
```

Expected: `mship phase review` succeeds (no warnings — tests are passing).

---

## Self-review (performed during plan authoring)

**Spec coverage:**
- §Design.1 diff status indicators → Task 1 (status detection), Task 2 (merge rule), Task 3 (rendering). ✓
- §Design.2 review zellij tab → Task 4. ✓
- §Design.3 spec fallback → Task 5 (with explicit sub-handling of missing-log-manager / no-task-resolvable edge cases). ✓
- §Testing strategy: every bullet maps to a named test in Tasks 1, 2, 3, 4, 5. ✓
- §Migration: FileDiff defaults preserve existing constructors (Task 1); SpecView default `log_manager=None` preserves existing test callers (Task 5). ✓
- §Out of scope: not implemented, as required.

**Placeholder scan:** Task 5 Step 1 contains one acknowledged uncertainty about the exact `Task` dataclass import path in `mship.core.state`. Handled with an inline fallback instruction — implement from whatever the actual class shape is. Everything else is concrete code.

**Type consistency:** `FileDiff` fields (`status`, `old_path`) consistent across Tasks 1-3. `SpecView` kwarg `log_manager` consistent across Task 5 constructor, helper, and test stubs. `_STATUS_STYLES` keyed by `"N"|"M"|"D"|"R"` matches detected status values.

All good. Ready to execute.

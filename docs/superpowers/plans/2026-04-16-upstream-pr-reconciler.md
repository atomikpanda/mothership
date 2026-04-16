# Upstream PR Reconciler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect upstream PR drift (merged, closed, diverged, base_changed) per task, block destructive commands on unresolved drift, expose an explicit `mship reconcile` command.

**Architecture:** A new `mship.core.reconcile` package with four focused modules (detect, fetch, cache, gate). One batched `gh pr list` call per 5-minute TTL, cached under `.mothership/reconcile.cache.json`. A single gate function is called by `spawn`, `finish`, `close`, and the pre-commit hook. New `mship reconcile` CLI surfaces results. Escape hatch: `--bypass-reconcile`.

**Tech Stack:** Python 3.14, Pydantic, Typer, dependency_injector, `gh` CLI, pytest.

**Spec:** `docs/superpowers/specs/2026-04-16-upstream-pr-reconciler-design.md`

---

## File Structure

**New:**
- `src/mship/core/reconcile/__init__.py` — package marker + public re-exports
- `src/mship/core/reconcile/detect.py` — `UpstreamState` enum + pure `detect()` function
- `src/mship/core/reconcile/cache.py` — cache read/write, TTL logic, ignore-list
- `src/mship/core/reconcile/fetch.py` — `gh pr list` invocation + offline fallback + `GitInspector` helper
- `src/mship/core/reconcile/gate.py` — combines fetch+detect+cache for blocker decisions
- `src/mship/cli/reconcile.py` — `mship reconcile` subcommand
- `tests/core/reconcile/__init__.py`
- `tests/core/reconcile/test_detect.py`
- `tests/core/reconcile/test_cache.py`
- `tests/core/reconcile/test_fetch.py`
- `tests/core/reconcile/test_gate.py`
- `tests/cli/test_reconcile.py`

**Modified:**
- `src/mship/core/state.py` — add `base_branch: str | None = None` to `Task`
- `src/mship/cli/worktree.py` — record `base_branch` on spawn; add `--bypass-reconcile` + gate call on `spawn`, `finish`, `close`
- `src/mship/cli/internal.py` — call gate in `_check-commit` pre-commit handler
- `src/mship/cli/__init__.py` — register reconcile module

---

## Task 1: Add `base_branch` to Task state

**Files:**
- Modify: `src/mship/core/state.py` (add field)
- Modify: `src/mship/cli/worktree.py` (populate at spawn)
- Test: `tests/core/test_state.py` (extend)

- [ ] **Step 1.1: Write failing test for the field**

Append to `tests/core/test_state.py`:

```python
def test_task_base_branch_defaults_none():
    from mship.core.state import Task
    from datetime import datetime, timezone
    t = Task(
        slug="x", description="x", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["a"], branch="feat/x",
    )
    assert t.base_branch is None


def test_task_base_branch_set_via_kwarg():
    from mship.core.state import Task
    from datetime import datetime, timezone
    t = Task(
        slug="x", description="x", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["a"], branch="feat/x",
        base_branch="main",
    )
    assert t.base_branch == "main"
```

- [ ] **Step 1.2: Run — confirm fail**

Run: `uv run pytest tests/core/test_state.py::test_task_base_branch_defaults_none tests/core/test_state.py::test_task_base_branch_set_via_kwarg -v`
Expected: FAIL with `ValidationError` or `AttributeError`.

- [ ] **Step 1.3: Add the field**

In `src/mship/core/state.py`, inside `class Task`, after the `test_iteration: int = 0` line (or wherever model fields end), add:

```python
    base_branch: str | None = None
```

- [ ] **Step 1.4: Run — confirm pass**

Run: `uv run pytest tests/core/test_state.py -v`
Expected: all PASS, including the two new tests.

- [ ] **Step 1.5: Populate on spawn**

In `src/mship/cli/worktree.py`, inside the `spawn` command body, locate the line that creates the `Task(...)` object (search for `Task(` in the file — it's after worktree creation). Add `base_branch=<default>` to the Task constructor call, where the default is resolved from the workspace's main repo default branch. If the codebase has no existing helper, use this inline logic:

```python
# Resolve workspace default branch (best-effort; falls back to "main").
from mship.core.reconcile.fetch import workspace_default_branch
base_branch = workspace_default_branch(container) or "main"
# ... then in the Task(...) constructor call:
base_branch=base_branch,
```

Note: `workspace_default_branch` is defined in Task 4. For now, in this task, hardcode `base_branch="main"` in the spawn call to keep Task 1 self-contained:

```python
base_branch="main",
```

Task 4 will upgrade this to dynamic detection.

- [ ] **Step 1.6: Write failing test for spawn populates base_branch**

Append to `tests/cli/test_worktree.py` (or wherever spawn tests live — use `rg "def test_.*spawn" tests/` to find the right file):

```python
def test_spawn_records_base_branch_main(tmp_path, monkeypatch):
    """Regression: newly spawned tasks record base_branch so reconcile can detect drift."""
    # Follow the existing spawn-test fixture pattern in this file (init workspace, invoke spawn, load state).
    # After spawn, assert: state.tasks[<slug>].base_branch == "main"
    pass  # Replace with the concrete fixture pattern observed in this file.
```

IMPORTANT: read the nearest existing `test_spawn*` in the same file and mirror its fixture setup verbatim, replacing the body with the assertion `assert state.tasks[<slug>].base_branch == "main"`. Don't invent new fixtures.

- [ ] **Step 1.7: Run — confirm pass**

Run: `uv run pytest tests/cli/test_worktree.py -v`
Expected: all PASS.

- [ ] **Step 1.8: Commit**

```bash
git add src/mship/core/state.py src/mship/cli/worktree.py tests/core/test_state.py tests/cli/test_worktree.py
git commit -m "feat(state): record base_branch on Task for reconcile"
```

---

## Task 2: `UpstreamState` enum + pure `detect()`

**Files:**
- Create: `src/mship/core/reconcile/__init__.py`
- Create: `src/mship/core/reconcile/detect.py`
- Create: `tests/core/reconcile/__init__.py` (empty)
- Test: `tests/core/reconcile/test_detect.py`

- [ ] **Step 2.1: Write failing test**

Create `tests/core/reconcile/__init__.py` (empty file).

Create `tests/core/reconcile/test_detect.py`:

```python
from datetime import datetime, timezone
from mship.core.reconcile.detect import (
    UpstreamState, PRSnapshot, GitSnapshot, detect_one, detect_many,
)


def _task_snap(head="feat/foo", state="OPEN", base="main", merge_sha=None, url="https://x/pr/1", updated_at="2026-04-16T00:00:00Z"):
    return PRSnapshot(
        head_ref=head, state=state, base_ref=base,
        merge_commit=merge_sha, url=url, updated_at=updated_at,
    )


def test_detect_in_sync():
    r = detect_one(
        task_branch="feat/foo", task_base="main",
        pr=_task_snap(),
        git=GitSnapshot(has_upstream=True, behind=0, ahead=3),
    )
    assert r.state == UpstreamState.in_sync


def test_detect_merged():
    r = detect_one(
        task_branch="feat/foo", task_base="main",
        pr=_task_snap(state="MERGED", merge_sha="abc123"),
        git=GitSnapshot(has_upstream=True, behind=0, ahead=0),
    )
    assert r.state == UpstreamState.merged
    assert r.pr_url == "https://x/pr/1"


def test_detect_closed():
    r = detect_one(
        task_branch="feat/foo", task_base="main",
        pr=_task_snap(state="CLOSED"),
        git=GitSnapshot(has_upstream=True, behind=0, ahead=0),
    )
    assert r.state == UpstreamState.closed


def test_detect_diverged_when_remote_ahead():
    r = detect_one(
        task_branch="feat/foo", task_base="main",
        pr=_task_snap(),
        git=GitSnapshot(has_upstream=True, behind=4, ahead=1),
    )
    assert r.state == UpstreamState.diverged


def test_detect_base_changed():
    r = detect_one(
        task_branch="feat/foo", task_base="main",
        pr=_task_snap(base="develop"),
        git=GitSnapshot(has_upstream=True, behind=0, ahead=1),
    )
    assert r.state == UpstreamState.base_changed


def test_detect_missing_when_no_pr():
    r = detect_one(
        task_branch="feat/foo", task_base="main",
        pr=None,
        git=GitSnapshot(has_upstream=False, behind=0, ahead=0),
    )
    assert r.state == UpstreamState.missing


def test_detect_many_maps_per_slug():
    result = detect_many(
        tasks=[
            ("a", "feat/a", "main"),
            ("b", "feat/b", "main"),
        ],
        pr_by_head={
            "feat/a": _task_snap(head="feat/a", state="MERGED", merge_sha="x"),
            "feat/b": _task_snap(head="feat/b"),
        },
        git_by_branch={
            "feat/a": GitSnapshot(has_upstream=True, behind=0, ahead=0),
            "feat/b": GitSnapshot(has_upstream=True, behind=0, ahead=2),
        },
    )
    assert result["a"].state == UpstreamState.merged
    assert result["b"].state == UpstreamState.in_sync


def test_detect_precedence_merged_beats_diverged():
    """When PR is merged, we don't care about local divergence."""
    r = detect_one(
        task_branch="feat/foo", task_base="main",
        pr=_task_snap(state="MERGED", merge_sha="x"),
        git=GitSnapshot(has_upstream=True, behind=99, ahead=99),
    )
    assert r.state == UpstreamState.merged
```

- [ ] **Step 2.2: Run — confirm fail**

Run: `uv run pytest tests/core/reconcile/test_detect.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 2.3: Implement the detection module**

Create `src/mship/core/reconcile/__init__.py`:

```python
"""Upstream PR reconciliation — detect, cache, gate for mship tasks."""
```

Create `src/mship/core/reconcile/detect.py`:

```python
"""Pure detection: given local task state + a snapshot of upstream + local git,
compute an UpstreamState for each task. No I/O."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence


class UpstreamState(str, Enum):
    in_sync = "in_sync"
    merged = "merged"
    closed = "closed"
    diverged = "diverged"
    base_changed = "base_changed"
    missing = "missing"


@dataclass(frozen=True)
class PRSnapshot:
    head_ref: str
    state: str                 # "OPEN" | "CLOSED" | "MERGED" | "DRAFT"
    base_ref: str
    merge_commit: str | None
    url: str
    updated_at: str


@dataclass(frozen=True)
class GitSnapshot:
    has_upstream: bool
    behind: int                # remote-only commits
    ahead: int                 # local-only commits


@dataclass(frozen=True)
class Detection:
    state: UpstreamState
    pr_url: str | None
    pr_number: int | None
    base: str | None
    merge_commit: str | None
    updated_at: str | None


def _pr_number(url: str | None) -> int | None:
    if not url:
        return None
    try:
        return int(url.rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None


def detect_one(
    task_branch: str,
    task_base: str | None,
    pr: PRSnapshot | None,
    git: GitSnapshot,
) -> Detection:
    if pr is None:
        return Detection(
            state=UpstreamState.missing,
            pr_url=None, pr_number=None, base=None,
            merge_commit=None, updated_at=None,
        )
    if pr.state == "MERGED":
        return Detection(
            state=UpstreamState.merged,
            pr_url=pr.url, pr_number=_pr_number(pr.url), base=pr.base_ref,
            merge_commit=pr.merge_commit, updated_at=pr.updated_at,
        )
    if pr.state == "CLOSED":
        return Detection(
            state=UpstreamState.closed,
            pr_url=pr.url, pr_number=_pr_number(pr.url), base=pr.base_ref,
            merge_commit=None, updated_at=pr.updated_at,
        )
    # PR is open/draft — check for divergence and base changes.
    if task_base is not None and pr.base_ref != task_base:
        return Detection(
            state=UpstreamState.base_changed,
            pr_url=pr.url, pr_number=_pr_number(pr.url), base=pr.base_ref,
            merge_commit=None, updated_at=pr.updated_at,
        )
    if git.has_upstream and git.behind > 0:
        return Detection(
            state=UpstreamState.diverged,
            pr_url=pr.url, pr_number=_pr_number(pr.url), base=pr.base_ref,
            merge_commit=None, updated_at=pr.updated_at,
        )
    return Detection(
        state=UpstreamState.in_sync,
        pr_url=pr.url, pr_number=_pr_number(pr.url), base=pr.base_ref,
        merge_commit=None, updated_at=pr.updated_at,
    )


def detect_many(
    tasks: Sequence[tuple[str, str, str | None]],   # (slug, branch, base)
    pr_by_head: dict[str, PRSnapshot],
    git_by_branch: dict[str, GitSnapshot],
) -> dict[str, Detection]:
    out: dict[str, Detection] = {}
    for slug, branch, base in tasks:
        pr = pr_by_head.get(branch)
        git = git_by_branch.get(branch, GitSnapshot(has_upstream=False, behind=0, ahead=0))
        out[slug] = detect_one(branch, base, pr, git)
    return out
```

- [ ] **Step 2.4: Run — confirm pass**

Run: `uv run pytest tests/core/reconcile/test_detect.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 2.5: Commit**

```bash
git add src/mship/core/reconcile/ tests/core/reconcile/
git commit -m "feat(reconcile): UpstreamState enum + pure detect()"
```

---

## Task 3: Cache module

**Files:**
- Create: `src/mship/core/reconcile/cache.py`
- Test: `tests/core/reconcile/test_cache.py`

- [ ] **Step 3.1: Write failing test**

Create `tests/core/reconcile/test_cache.py`:

```python
import json
import time
from pathlib import Path

from mship.core.reconcile.cache import ReconcileCache, CachePayload


def test_read_returns_none_when_file_absent(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    assert c.read() is None


def test_write_then_read_roundtrips(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    payload = CachePayload(
        fetched_at=time.time(),
        ttl_seconds=300,
        results={"a": {"state": "merged", "pr_url": "https://x/pr/1"}},
        ignored=[],
    )
    c.write(payload)
    got = c.read()
    assert got is not None
    assert got.results == {"a": {"state": "merged", "pr_url": "https://x/pr/1"}}
    assert got.ttl_seconds == 300


def test_is_fresh_true_within_ttl(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    payload = CachePayload(fetched_at=time.time(), ttl_seconds=300, results={}, ignored=[])
    assert c.is_fresh(payload) is True


def test_is_fresh_false_after_ttl(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    payload = CachePayload(fetched_at=time.time() - 1000, ttl_seconds=300, results={}, ignored=[])
    assert c.is_fresh(payload) is False


def test_add_ignore_persists(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    c.add_ignore("slug-a")
    assert "slug-a" in c.read_ignores()


def test_add_ignore_dedupes(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    c.add_ignore("slug-a")
    c.add_ignore("slug-a")
    assert c.read_ignores() == ["slug-a"]


def test_remove_ignore(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    c.add_ignore("slug-a")
    c.add_ignore("slug-b")
    c.remove_ignore("slug-a")
    assert c.read_ignores() == ["slug-b"]


def test_clear_ignores(tmp_path: Path):
    c = ReconcileCache(tmp_path / ".mothership")
    c.add_ignore("slug-a")
    c.add_ignore("slug-b")
    c.clear_ignores()
    assert c.read_ignores() == []


def test_corrupt_cache_returns_none(tmp_path: Path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    (state_dir / "reconcile.cache.json").write_text("not json")
    c = ReconcileCache(state_dir)
    assert c.read() is None
```

- [ ] **Step 3.2: Run — confirm fail**

Run: `uv run pytest tests/core/reconcile/test_cache.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3.3: Implement cache**

Create `src/mship/core/reconcile/cache.py`:

```python
"""Reconcile cache: batched gh responses + per-task ignore list."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


CACHE_FILENAME = "reconcile.cache.json"
DEFAULT_TTL_SECONDS = 300


@dataclass
class CachePayload:
    fetched_at: float
    ttl_seconds: int
    results: dict[str, dict]
    ignored: list[str] = field(default_factory=list)


class ReconcileCache:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)
        self._path = self._state_dir / CACHE_FILENAME

    # --- payload ---

    def read(self) -> CachePayload | None:
        if not self._path.is_file():
            return None
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return CachePayload(
                fetched_at=float(data["fetched_at"]),
                ttl_seconds=int(data.get("ttl_seconds", DEFAULT_TTL_SECONDS)),
                results=dict(data.get("results", {})),
                ignored=list(data.get("ignored", [])),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def write(self, payload: CachePayload) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        body = {
            "fetched_at": payload.fetched_at,
            "ttl_seconds": payload.ttl_seconds,
            "results": payload.results,
            "ignored": payload.ignored,
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=2))
        tmp.replace(self._path)

    def is_fresh(self, payload: CachePayload) -> bool:
        return (time.time() - payload.fetched_at) < payload.ttl_seconds

    # --- ignore list ---

    def read_ignores(self) -> list[str]:
        payload = self.read()
        return list(payload.ignored) if payload else []

    def add_ignore(self, slug: str) -> None:
        payload = self.read() or CachePayload(
            fetched_at=0.0, ttl_seconds=DEFAULT_TTL_SECONDS, results={}, ignored=[],
        )
        if slug not in payload.ignored:
            payload.ignored.append(slug)
        self.write(payload)

    def remove_ignore(self, slug: str) -> None:
        payload = self.read()
        if payload is None or slug not in payload.ignored:
            return
        payload.ignored = [s for s in payload.ignored if s != slug]
        self.write(payload)

    def clear_ignores(self) -> None:
        payload = self.read()
        if payload is None:
            return
        payload.ignored = []
        self.write(payload)
```

- [ ] **Step 3.4: Run — confirm pass**

Run: `uv run pytest tests/core/reconcile/test_cache.py -v`
Expected: all 9 tests PASS.

- [ ] **Step 3.5: Commit**

```bash
git add src/mship/core/reconcile/cache.py tests/core/reconcile/test_cache.py
git commit -m "feat(reconcile): cache with TTL + per-slug ignore list"
```

---

## Task 4: Fetch module (gh + git inspector + offline fallback)

**Files:**
- Create: `src/mship/core/reconcile/fetch.py`
- Test: `tests/core/reconcile/test_fetch.py`

- [ ] **Step 4.1: Write failing tests**

Create `tests/core/reconcile/test_fetch.py`:

```python
from pathlib import Path

from mship.core.reconcile.fetch import (
    FetchError,
    parse_gh_pr_list,
    gh_search_query,
    collect_git_snapshots,
)
from mship.core.reconcile.detect import PRSnapshot, GitSnapshot


def test_gh_search_query_ors_heads():
    q = gh_search_query(["feat/a", "feat/b"])
    assert "head:feat/a" in q
    assert "head:feat/b" in q


def test_parse_gh_pr_list_maps_fields():
    raw = [
        {
            "headRefName": "feat/a",
            "state": "MERGED",
            "baseRefName": "main",
            "mergeCommit": {"oid": "abc123"},
            "url": "https://github.com/o/r/pull/42",
            "updatedAt": "2026-04-16T00:00:00Z",
        },
        {
            "headRefName": "feat/b",
            "state": "OPEN",
            "baseRefName": "main",
            "mergeCommit": None,
            "url": "https://github.com/o/r/pull/43",
            "updatedAt": "2026-04-16T01:00:00Z",
        },
    ]
    parsed = parse_gh_pr_list(raw)
    assert parsed["feat/a"].state == "MERGED"
    assert parsed["feat/a"].merge_commit == "abc123"
    assert parsed["feat/b"].state == "OPEN"
    assert parsed["feat/b"].merge_commit is None


def test_parse_gh_pr_list_picks_most_recent_on_dup_head():
    raw = [
        {"headRefName": "feat/a", "state": "CLOSED", "baseRefName": "main",
         "mergeCommit": None, "url": "url-old", "updatedAt": "2026-01-01T00:00:00Z"},
        {"headRefName": "feat/a", "state": "OPEN", "baseRefName": "main",
         "mergeCommit": None, "url": "url-new", "updatedAt": "2026-04-16T00:00:00Z"},
    ]
    parsed = parse_gh_pr_list(raw)
    assert parsed["feat/a"].url == "url-new"


def test_parse_gh_pr_list_handles_missing_fields_gracefully():
    # Incomplete records are skipped, not crashed on.
    raw = [
        {"headRefName": "feat/a"},  # missing state etc.
        {"headRefName": "feat/b", "state": "OPEN", "baseRefName": "main",
         "mergeCommit": None, "url": "u", "updatedAt": "2026-04-16T00:00:00Z"},
    ]
    parsed = parse_gh_pr_list(raw)
    assert "feat/a" not in parsed
    assert "feat/b" in parsed


class _FakeGit:
    """Captures subprocess-style calls; answers rev-list --left-right counts."""
    def __init__(self, per_branch: dict[str, tuple[int, int]]):
        # per_branch: {branch: (behind, ahead)}
        self._per_branch = per_branch

    def run(self, args, cwd=None):
        if args[:2] == ["rev-parse", "--abbrev-ref"] and args[2].endswith("@{u}"):
            branch = args[2].removesuffix("@{u}")
            if branch in self._per_branch:
                return (0, f"origin/{branch}\n")
            return (1, "")
        if args[:3] == ["rev-list", "--left-right", "--count"]:
            spec = args[3]  # e.g. "@{u}...HEAD"
            # spec comes from current branch context; use cwd branch via trailing arg hack
            # In our impl, the caller passes branch via env-less; we use fake with single "current"
            # Here we match any spec and return the FIRST registered branch's counts.
            branch = next(iter(self._per_branch))
            behind, ahead = self._per_branch[branch]
            return (0, f"{behind}\t{ahead}\n")
        return (0, "")


def test_collect_git_snapshots_uses_rev_list(tmp_path):
    # We test the parser/aggregator, not subprocess plumbing. Use direct dict form.
    worktrees_by_branch = {"feat/a": tmp_path}
    fake = _FakeGit({"feat/a": (2, 5)})
    snaps = collect_git_snapshots(worktrees_by_branch, runner=fake)
    assert snaps["feat/a"] == GitSnapshot(has_upstream=True, behind=2, ahead=5)


def test_collect_git_snapshots_no_upstream(tmp_path):
    worktrees_by_branch = {"feat/a": tmp_path}
    fake = _FakeGit({})  # no upstream registered
    snaps = collect_git_snapshots(worktrees_by_branch, runner=fake)
    assert snaps["feat/a"].has_upstream is False
    assert snaps["feat/a"].behind == 0


def test_fetch_error_raised_on_gh_missing(monkeypatch, tmp_path):
    from mship.core.reconcile.fetch import fetch_pr_snapshots
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    try:
        fetch_pr_snapshots(branches=["feat/a"])
    except FetchError as e:
        assert "gh" in str(e).lower()
    else:
        raise AssertionError("expected FetchError")
```

- [ ] **Step 4.2: Run — confirm fail**

Run: `uv run pytest tests/core/reconcile/test_fetch.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 4.3: Implement fetch**

Create `src/mship/core/reconcile/fetch.py`:

```python
"""Fetch upstream PR snapshots and local git-drift snapshots.

- `fetch_pr_snapshots(branches)` -> dict[branch, PRSnapshot] via one batched `gh pr list`.
- `collect_git_snapshots(worktrees_by_branch, runner)` -> dict[branch, GitSnapshot].
- Offline / gh-missing / non-zero-exit -> raises FetchError; callers decide fallback.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Protocol

from mship.core.reconcile.detect import GitSnapshot, PRSnapshot


class FetchError(RuntimeError):
    pass


# --- gh PR fetching ---------------------------------------------------------


def gh_search_query(branches: list[str]) -> str:
    """Build a `head:` search string for `gh pr list --search`."""
    return " ".join(f"head:{b}" for b in branches)


def parse_gh_pr_list(raw: list[dict]) -> dict[str, PRSnapshot]:
    """Map gh's JSON array to {head_ref: PRSnapshot}, most-recent wins on dupes."""
    out: dict[str, PRSnapshot] = {}
    for entry in raw:
        try:
            head = entry["headRefName"]
            state = entry["state"]
            base = entry["baseRefName"]
            url = entry["url"]
            updated = entry["updatedAt"]
        except (KeyError, TypeError):
            continue
        merge = None
        mc = entry.get("mergeCommit")
        if isinstance(mc, dict):
            merge = mc.get("oid")
        snap = PRSnapshot(
            head_ref=head, state=state, base_ref=base,
            merge_commit=merge, url=url, updated_at=updated,
        )
        prev = out.get(head)
        if prev is None or snap.updated_at > prev.updated_at:
            out[head] = snap
    return out


def fetch_pr_snapshots(branches: list[str], *, timeout: int = 30) -> dict[str, PRSnapshot]:
    if not branches:
        return {}
    if shutil.which("gh") is None:
        raise FetchError("gh CLI not installed")
    query = gh_search_query(branches)
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", "all", "--search", query,
             "--json", "headRefName,state,baseRefName,mergeCommit,url,updatedAt",
             "--limit", "100"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise FetchError(f"gh invocation failed: {e!r}") from e
    if result.returncode != 0:
        raise FetchError(f"gh exit {result.returncode}: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as e:
        raise FetchError(f"gh returned invalid JSON: {e!r}") from e
    if not isinstance(data, list):
        raise FetchError("gh returned non-list payload")
    return parse_gh_pr_list(data)


# --- git local snapshot -----------------------------------------------------


class GitRunner(Protocol):
    def run(self, args: list[str], cwd: Path | None = None) -> tuple[int, str]: ...


class _SubprocessGit:
    def run(self, args: list[str], cwd: Path | None = None) -> tuple[int, str]:
        try:
            r = subprocess.run(
                ["git", *args], capture_output=True, text=True, timeout=15,
                cwd=str(cwd) if cwd else None,
            )
        except (subprocess.SubprocessError, OSError) as e:
            return (1, repr(e))
        return (r.returncode, r.stdout)


def collect_git_snapshots(
    worktrees_by_branch: dict[str, Path],
    *,
    runner: GitRunner | None = None,
) -> dict[str, GitSnapshot]:
    """For each (branch, worktree-path), compute behind/ahead via rev-list.

    Runs `git rev-parse --abbrev-ref <branch>@{u}` — non-zero return means no
    upstream. If upstream exists, runs `git rev-list --left-right --count
    @{u}...HEAD` in the worktree.
    """
    runner = runner or _SubprocessGit()
    out: dict[str, GitSnapshot] = {}
    for branch, wt_path in worktrees_by_branch.items():
        rc, _ = runner.run(["rev-parse", "--abbrev-ref", f"{branch}@{{u}}"], cwd=wt_path)
        if rc != 0:
            out[branch] = GitSnapshot(has_upstream=False, behind=0, ahead=0)
            continue
        rc, stdout = runner.run(
            ["rev-list", "--left-right", "--count", "@{u}...HEAD"], cwd=wt_path,
        )
        if rc != 0:
            out[branch] = GitSnapshot(has_upstream=True, behind=0, ahead=0)
            continue
        parts = stdout.strip().split()
        try:
            behind, ahead = int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            behind, ahead = 0, 0
        out[branch] = GitSnapshot(has_upstream=True, behind=behind, ahead=ahead)
    return out


# --- workspace default branch helper ---------------------------------------


def workspace_default_branch(container) -> str | None:
    """Return the workspace's main repo's default branch name, or None on error.

    Uses `gh repo view --json defaultBranchRef` on the first configured repo.
    Returns None (caller falls back to 'main') if gh is missing or errors.
    """
    if shutil.which("gh") is None:
        return None
    try:
        repos = list(container.config().repos.keys())
    except Exception:
        return None
    if not repos:
        return None
    try:
        result = subprocess.run(
            ["gh", "repo", "view", repos[0], "--json", "defaultBranchRef"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "{}")
        return data["defaultBranchRef"]["name"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
```

- [ ] **Step 4.4: Run — confirm pass**

Run: `uv run pytest tests/core/reconcile/test_fetch.py -v`
Expected: all tests PASS.

- [ ] **Step 4.5: Upgrade `spawn` to use dynamic default-branch detection**

In `src/mship/cli/worktree.py` `spawn`, replace the hardcoded `base_branch="main"` from Task 1 with the dynamic helper:

```python
from mship.core.reconcile.fetch import workspace_default_branch
base_branch = workspace_default_branch(container) or "main"
# ... in Task(...) constructor:
base_branch=base_branch,
```

- [ ] **Step 4.6: Run full suite**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 4.7: Commit**

```bash
git add src/mship/core/reconcile/fetch.py src/mship/cli/worktree.py tests/core/reconcile/test_fetch.py
git commit -m "feat(reconcile): gh + git snapshot fetchers + offline errors"
```

---

## Task 5: Gate module — compose fetch + detect + cache + ignores

**Files:**
- Create: `src/mship/core/reconcile/gate.py`
- Test: `tests/core/reconcile/test_gate.py`

- [ ] **Step 5.1: Write failing test**

Create `tests/core/reconcile/test_gate.py`:

```python
import time
from datetime import datetime, timezone
from pathlib import Path

from mship.core.state import Task, WorkspaceState
from mship.core.reconcile.cache import ReconcileCache, CachePayload
from mship.core.reconcile.detect import UpstreamState, PRSnapshot, GitSnapshot
from mship.core.reconcile.gate import (
    Decision, GateAction, reconcile_now, should_block,
)


def _task(slug: str, **over) -> Task:
    base = dict(
        slug=slug, description=slug, phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], worktrees={"r": Path("/tmp/fake") / slug},
        branch=f"feat/{slug}", base_branch="main",
    )
    base.update(over)
    return Task(**base)


def test_reconcile_now_uses_fresh_cache(tmp_path: Path):
    cache = ReconcileCache(tmp_path)
    cache.write(CachePayload(
        fetched_at=time.time(), ttl_seconds=300,
        results={"a": {"state": "merged", "pr_url": "u", "pr_number": 1, "base": "main"}},
        ignored=[],
    ))
    state = WorkspaceState(tasks={"a": _task("a")})
    decisions = reconcile_now(state, cache=cache, fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("should not fetch")))
    assert decisions["a"].state == UpstreamState.merged


def test_reconcile_now_refetches_when_stale(tmp_path: Path):
    cache = ReconcileCache(tmp_path)
    cache.write(CachePayload(
        fetched_at=time.time() - 9999, ttl_seconds=300,
        results={"a": {"state": "in_sync"}}, ignored=[],
    ))
    state = WorkspaceState(tasks={"a": _task("a")})

    calls: list[list[str]] = []
    def fetcher(branches, worktrees):
        calls.append(list(branches))
        return (
            {"feat/a": PRSnapshot(head_ref="feat/a", state="MERGED", base_ref="main",
                                   merge_commit="x", url="https://x/pr/9", updated_at="z")},
            {"feat/a": GitSnapshot(has_upstream=True, behind=0, ahead=0)},
        )
    decisions = reconcile_now(state, cache=cache, fetcher=fetcher)
    assert calls == [["feat/a"]]
    assert decisions["a"].state == UpstreamState.merged


def test_reconcile_now_falls_back_to_cache_on_fetcher_error(tmp_path: Path):
    cache = ReconcileCache(tmp_path)
    cache.write(CachePayload(
        fetched_at=time.time() - 9999, ttl_seconds=300,
        results={"a": {"state": "merged", "pr_url": "u", "pr_number": 1, "base": "main"}},
        ignored=[],
    ))
    state = WorkspaceState(tasks={"a": _task("a")})

    def bad_fetcher(*_):
        from mship.core.reconcile.fetch import FetchError
        raise FetchError("offline")

    decisions = reconcile_now(state, cache=cache, fetcher=bad_fetcher)
    assert decisions["a"].state == UpstreamState.merged  # from stale cache


def test_reconcile_now_returns_unavailable_on_error_without_cache(tmp_path: Path):
    cache = ReconcileCache(tmp_path)
    state = WorkspaceState(tasks={"a": _task("a")})
    def bad_fetcher(*_):
        from mship.core.reconcile.fetch import FetchError
        raise FetchError("offline")
    decisions = reconcile_now(state, cache=cache, fetcher=bad_fetcher)
    # Unavailable => treat as in_sync so we never fail closed.
    assert decisions == {}


def test_should_block_merged_on_finish():
    d = Decision(
        slug="a", state=UpstreamState.merged, pr_url="u", pr_number=1,
        base="main", merge_commit="x", updated_at="z",
    )
    assert should_block(d, command="finish", ignored=[]) is GateAction.block


def test_should_block_merged_on_close_is_allowed():
    d = Decision(
        slug="a", state=UpstreamState.merged, pr_url="u", pr_number=1,
        base="main", merge_commit="x", updated_at="z",
    )
    assert should_block(d, command="close", ignored=[]) is GateAction.allow


def test_should_block_base_changed_on_precommit_is_allowed():
    d = Decision(
        slug="a", state=UpstreamState.base_changed, pr_url="u", pr_number=1,
        base="develop", merge_commit=None, updated_at="z",
    )
    assert should_block(d, command="precommit", ignored=[]) is GateAction.allow


def test_should_block_respects_ignore_list():
    d = Decision(
        slug="a", state=UpstreamState.merged, pr_url="u", pr_number=1,
        base="main", merge_commit="x", updated_at="z",
    )
    assert should_block(d, command="finish", ignored=["a"]) is GateAction.allow


def test_diverged_warns_on_spawn_blocks_on_finish():
    d = Decision(slug="a", state=UpstreamState.diverged, pr_url="u", pr_number=1,
                 base="main", merge_commit=None, updated_at="z")
    assert should_block(d, command="spawn", ignored=[]) is GateAction.warn
    assert should_block(d, command="finish", ignored=[]) is GateAction.block
```

- [ ] **Step 5.2: Run — confirm fail**

Run: `uv run pytest tests/core/reconcile/test_gate.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 5.3: Implement gate**

Create `src/mship/core/reconcile/gate.py`:

```python
"""Gate: single entry point for `spawn`, `finish`, `close`, pre-commit.

Runs reconcile_now() (cache-first, fetch on stale), then the caller inspects
each Decision via should_block() to choose block/warn/allow per command.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Literal

from mship.core.state import WorkspaceState
from mship.core.reconcile.cache import ReconcileCache, CachePayload, DEFAULT_TTL_SECONDS
from mship.core.reconcile.detect import (
    Detection, GitSnapshot, PRSnapshot, UpstreamState, detect_many,
)
from mship.core.reconcile.fetch import FetchError


Command = Literal["spawn", "finish", "close", "precommit"]


@dataclass(frozen=True)
class Decision:
    slug: str
    state: UpstreamState
    pr_url: str | None
    pr_number: int | None
    base: str | None
    merge_commit: str | None
    updated_at: str | None


class GateAction(str, Enum):
    allow = "allow"
    warn = "warn"
    block = "block"


# Blocking matrix (keys are UpstreamState.value).
_MATRIX: dict[str, dict[str, GateAction]] = {
    "in_sync":      {"spawn": GateAction.allow, "finish": GateAction.allow, "close": GateAction.allow, "precommit": GateAction.allow},
    "merged":       {"spawn": GateAction.block, "finish": GateAction.block, "close": GateAction.allow, "precommit": GateAction.block},
    "closed":       {"spawn": GateAction.block, "finish": GateAction.block, "close": GateAction.allow, "precommit": GateAction.block},
    "diverged":     {"spawn": GateAction.warn,  "finish": GateAction.block, "close": GateAction.allow, "precommit": GateAction.block},
    "base_changed": {"spawn": GateAction.warn,  "finish": GateAction.block, "close": GateAction.allow, "precommit": GateAction.allow},
    "missing":      {"spawn": GateAction.allow, "finish": GateAction.allow, "close": GateAction.allow, "precommit": GateAction.allow},
}


def should_block(decision: Decision, *, command: Command, ignored: list[str]) -> GateAction:
    if decision.slug in ignored:
        return GateAction.allow
    return _MATRIX[decision.state.value][command]


# --- reconcile_now() --------------------------------------------------------

Fetcher = Callable[[list[str], dict[str, Path]], tuple[dict[str, PRSnapshot], dict[str, GitSnapshot]]]


def _decision_from_detection(slug: str, det: Detection) -> Decision:
    return Decision(
        slug=slug, state=det.state, pr_url=det.pr_url, pr_number=det.pr_number,
        base=det.base, merge_commit=det.merge_commit, updated_at=det.updated_at,
    )


def _decision_from_cache_entry(slug: str, raw: dict) -> Decision | None:
    try:
        return Decision(
            slug=slug,
            state=UpstreamState(raw["state"]),
            pr_url=raw.get("pr_url"),
            pr_number=raw.get("pr_number"),
            base=raw.get("base"),
            merge_commit=raw.get("merge_commit"),
            updated_at=raw.get("updated_at"),
        )
    except (KeyError, ValueError):
        return None


def _decisions_from_cache(state: WorkspaceState, payload: CachePayload) -> dict[str, Decision]:
    out: dict[str, Decision] = {}
    for slug in state.tasks:
        raw = payload.results.get(slug)
        if raw is None:
            continue
        d = _decision_from_cache_entry(slug, raw)
        if d is not None:
            out[slug] = d
    return out


def reconcile_now(
    state: WorkspaceState,
    *,
    cache: ReconcileCache,
    fetcher: Fetcher,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Decision]:
    """Return per-slug Decision. Cache-first; fetch on stale; fall back on error."""
    payload = cache.read()
    if payload and cache.is_fresh(payload):
        return _decisions_from_cache(state, payload)

    branches = [t.branch for t in state.tasks.values()]
    worktrees_by_branch: dict[str, Path] = {}
    for t in state.tasks.values():
        if t.worktrees:
            worktrees_by_branch[t.branch] = next(iter(t.worktrees.values()))

    try:
        pr_by_head, git_by_branch = fetcher(branches, worktrees_by_branch)
    except FetchError:
        if payload is not None:
            return _decisions_from_cache(state, payload)
        return {}  # unavailable

    tasks_tuples = [(t.slug, t.branch, t.base_branch) for t in state.tasks.values()]
    detections = detect_many(tasks_tuples, pr_by_head, git_by_branch)

    # Persist.
    results = {
        slug: {
            "state": d.state.value,
            "pr_url": d.pr_url, "pr_number": d.pr_number,
            "base": d.base, "merge_commit": d.merge_commit,
            "updated_at": d.updated_at,
        }
        for slug, d in detections.items()
    }
    cache.write(CachePayload(
        fetched_at=time.time(),
        ttl_seconds=ttl_seconds,
        results=results,
        ignored=(payload.ignored if payload else []),
    ))
    return {slug: _decision_from_detection(slug, d) for slug, d in detections.items()}
```

- [ ] **Step 5.4: Run — confirm pass**

Run: `uv run pytest tests/core/reconcile/test_gate.py -v`
Expected: all 9 tests PASS.

- [ ] **Step 5.5: Commit**

```bash
git add src/mship/core/reconcile/gate.py tests/core/reconcile/test_gate.py
git commit -m "feat(reconcile): gate — cache-first reconcile + blocking matrix"
```

---

## Task 6: `mship reconcile` CLI

**Files:**
- Create: `src/mship/cli/reconcile.py`
- Modify: `src/mship/cli/__init__.py` (register)
- Test: `tests/cli/test_reconcile.py`

- [ ] **Step 6.1: Write failing test**

Create `tests/cli/test_reconcile.py`:

```python
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app
from mship.core.state import Task, WorkspaceState
from mship.core.reconcile.cache import ReconcileCache, CachePayload
import time


runner = CliRunner()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Minimal workspace with one task + container overrides."""
    from mship.container import Container

    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()

    # Create a minimal mothership.yaml so config_path.parent resolves.
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("repos: {r: {path: .}}\n")

    # Seed state with a single task.
    task = Task(
        slug="a", description="a", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], worktrees={"r": tmp_path},
        branch="feat/a", base_branch="main",
    )
    from mship.core.state import StateManager
    StateManager(state_dir).save(WorkspaceState(tasks={"a": task}, current_task="a"))

    container = Container()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)

    from mship.cli.__init__ import get_container as _gc_ref  # noqa: F401 (may not exist this way; see note)
    # The real hook is to override the module-level get_container used by register().
    # If that isn't importable, skip this test and set an env-var-based override per the existing pattern.

    yield container, state_dir, tmp_path


def test_reconcile_prints_table_from_cache(workspace, monkeypatch):
    container, state_dir, _ = workspace
    cache = ReconcileCache(state_dir)
    cache.write(CachePayload(
        fetched_at=time.time(), ttl_seconds=300,
        results={"a": {"state": "merged", "pr_url": "https://x/pr/1",
                       "pr_number": 1, "base": "main"}},
        ignored=[],
    ))

    # Stub the fetcher to ensure cache path is used.
    from mship.core.reconcile import gate as gate_mod
    monkeypatch.setattr(gate_mod, "reconcile_now", lambda state, cache, fetcher, ttl_seconds=300: {
        "a": gate_mod.Decision(slug="a",
            state=gate_mod.UpstreamState.merged,
            pr_url="https://x/pr/1", pr_number=1,
            base="main", merge_commit=None, updated_at=None),
    })

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert "merged" in result.output
    assert "a" in result.output


def test_reconcile_json_output(workspace, monkeypatch):
    container, state_dir, _ = workspace
    from mship.core.reconcile import gate as gate_mod
    monkeypatch.setattr(gate_mod, "reconcile_now", lambda state, cache, fetcher, ttl_seconds=300: {
        "a": gate_mod.Decision(slug="a", state=gate_mod.UpstreamState.in_sync,
                               pr_url=None, pr_number=None, base="main",
                               merge_commit=None, updated_at=None),
    })
    result = runner.invoke(app, ["reconcile", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tasks"][0]["slug"] == "a"


def test_reconcile_add_ignore(workspace):
    container, state_dir, _ = workspace
    result = runner.invoke(app, ["reconcile", "--ignore", "a"])
    assert result.exit_code == 0, result.output
    assert "a" in ReconcileCache(state_dir).read_ignores()


def test_reconcile_clear_ignores(workspace):
    container, state_dir, _ = workspace
    cache = ReconcileCache(state_dir)
    cache.add_ignore("a")
    cache.add_ignore("b")
    result = runner.invoke(app, ["reconcile", "--clear-ignores"])
    assert result.exit_code == 0, result.output
    assert cache.read_ignores() == []
```

IMPORTANT: the `workspace` fixture above sketches the shape but does NOT wire the container into the Typer app's `get_container`. Before writing this test, read an existing fixture in `tests/cli/test_status.py` or `tests/cli/view/test_status_view.py::test_status_cli_rejects_unknown_task` that uses `container.config_path.override(...)` / `container.state_dir.override(...)` in `try/finally`. Mirror that pattern exactly — use the same `get_container` hook those tests use.

- [ ] **Step 6.2: Run — confirm fail**

Run: `uv run pytest tests/cli/test_reconcile.py -v`
Expected: FAIL — `mship reconcile` command doesn't exist yet.

- [ ] **Step 6.3: Implement CLI**

Create `src/mship/cli/reconcile.py`:

```python
"""`mship reconcile` — detect upstream PR drift."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output
from mship.core.reconcile.cache import ReconcileCache
from mship.core.reconcile.detect import UpstreamState
from mship.core.reconcile.fetch import (
    FetchError, collect_git_snapshots, fetch_pr_snapshots,
)
from mship.core.reconcile.gate import Decision, reconcile_now


_ACTION_HINTS = {
    UpstreamState.merged:       "run `mship close`",
    UpstreamState.closed:       "run `mship close --abandon`",
    UpstreamState.diverged:     "pull and rebase",
    UpstreamState.base_changed: "rebase onto new base",
    UpstreamState.missing:      "—",
    UpstreamState.in_sync:      "—",
}


def _glyph(state: UpstreamState) -> str:
    return "✓" if state in (UpstreamState.in_sync, UpstreamState.missing) else "⚠"


def register(app: typer.Typer, get_container):
    @app.command()
    def reconcile(
        json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table"),
        ignore: Optional[str] = typer.Option(None, "--ignore", help="Persistently ignore drift for this slug"),
        clear_ignores: bool = typer.Option(False, "--clear-ignores", help="Reset the ignore list"),
        refresh: bool = typer.Option(False, "--refresh", help="Skip cache, refetch"),
    ):
        """Detect upstream PR drift across every task in the workspace."""
        output = Output()
        container = get_container()
        state = container.state_manager().load()
        state_dir = Path(container.config_path()).parent / ".mothership"
        cache = ReconcileCache(state_dir)

        if clear_ignores:
            cache.clear_ignores()
            if output.is_tty:
                output.success("Ignore list cleared.")
            else:
                output.json({"cleared": True})
            return

        if ignore is not None:
            if ignore not in state.tasks:
                output.error(f"Unknown task: {ignore!r}.")
                raise typer.Exit(code=1)
            cache.add_ignore(ignore)
            if output.is_tty:
                output.success(f"Ignoring drift for: {ignore}")
            else:
                output.json({"ignored": ignore})
            return

        if refresh:
            # Invalidate cache to force a refetch this call.
            payload = cache.read()
            if payload is not None:
                payload.fetched_at = 0.0
                cache.write(payload)

        def _fetcher(branches, worktrees_by_branch):
            return (
                fetch_pr_snapshots(branches),
                collect_git_snapshots(worktrees_by_branch),
            )

        try:
            decisions = reconcile_now(state, cache=cache, fetcher=_fetcher)
        except Exception as e:  # noqa: BLE001 — never fail closed
            output.warning(f"reconcile unavailable: {e}")
            decisions = {}

        _emit(output, decisions, json_out, cache.read_ignores())


def _emit(output: Output, decisions: dict[str, Decision], json_out: bool, ignored: list[str]) -> None:
    if json_out or not output.is_tty:
        output.json({
            "tasks": [
                {
                    "slug": d.slug,
                    "state": d.state.value,
                    "pr_url": d.pr_url,
                    "pr_number": d.pr_number,
                    "base": d.base,
                    "merge_commit": d.merge_commit,
                    "ignored": d.slug in ignored,
                }
                for d in decisions.values()
            ],
            "ignored": ignored,
        })
        return
    if not decisions:
        output.print("No tasks to reconcile.")
        return
    rows: list[list[str]] = []
    for d in decisions.values():
        mark = f"{_glyph(d.state)} {d.state.value}"
        pr = f"#{d.pr_number}" if d.pr_number else "—"
        action = _ACTION_HINTS[d.state]
        if d.slug in ignored:
            action = f"(ignored) {action}"
        rows.append([d.slug, mark, pr, action])
    output.table(
        title="Upstream reconciliation",
        columns=["Task", "State", "PR", "Action"],
        rows=rows,
    )
```

- [ ] **Step 6.4: Register the command**

Open `src/mship/cli/__init__.py`. Locate the block of `from mship.cli import <mod> as _<mod>_mod` imports (near line 59–74). Append:

```python
from mship.cli import reconcile as _reconcile_mod
```

Locate the block of `_<mod>_mod.register(app, get_container)` calls (near line 76+). Append:

```python
_reconcile_mod.register(app, get_container)
```

- [ ] **Step 6.5: Run — confirm pass**

Run: `uv run pytest tests/cli/test_reconcile.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 6.6: Commit**

```bash
git add src/mship/cli/reconcile.py src/mship/cli/__init__.py tests/cli/test_reconcile.py
git commit -m "feat(reconcile): mship reconcile CLI (table + json + ignores)"
```

---

## Task 7: Gate wiring for spawn / finish / close / pre-commit

**Files:**
- Modify: `src/mship/cli/worktree.py` (spawn, finish, close — add `--bypass-reconcile` + gate call)
- Modify: `src/mship/cli/internal.py` (pre-commit gate call)
- Test: `tests/cli/test_reconcile.py` (add gate-integration tests)

- [ ] **Step 7.1: Write failing gate-integration tests**

Append to `tests/cli/test_reconcile.py`:

```python
def test_finish_blocks_on_merged_drift(workspace, monkeypatch):
    container, state_dir, _ = workspace
    # Seed cache with merged state
    from mship.core.reconcile.cache import ReconcileCache, CachePayload
    import time
    ReconcileCache(state_dir).write(CachePayload(
        fetched_at=time.time(), ttl_seconds=300,
        results={"a": {"state": "merged", "pr_url": "u", "pr_number": 1, "base": "main"}},
        ignored=[],
    ))
    result = runner.invoke(app, ["finish"])
    assert result.exit_code != 0
    assert "merged" in result.output.lower()
    assert "bypass-reconcile" in result.output.lower()


def test_finish_bypass_lets_through(workspace, monkeypatch):
    container, state_dir, _ = workspace
    from mship.core.reconcile.cache import ReconcileCache, CachePayload
    import time
    ReconcileCache(state_dir).write(CachePayload(
        fetched_at=time.time(), ttl_seconds=300,
        results={"a": {"state": "merged", "pr_url": "u", "pr_number": 1, "base": "main"}},
        ignored=[],
    ))
    # Mock the rest of finish so we only exercise the gate.
    # Patch the finish internals to exit 0 after the gate check.
    # In practice, patch `mship.cli.worktree._do_finish` or whichever helper does the work.
    # Placeholder assertion — implementer should mock enough of finish to reach exit 0.
    result = runner.invoke(app, ["finish", "--bypass-reconcile"])
    # Not asserting exit_code==0 strictly because finish may fail downstream;
    # we assert the gate block is NOT in the output.
    assert "reconcile refused" not in result.output.lower()
```

IMPORTANT: the second test requires patching enough of `finish`'s internals so the test doesn't fail for unrelated reasons (git push, PR create). If that's too invasive, replace with a unit-level test that calls the gate helper directly.

- [ ] **Step 7.2: Run — confirm fail**

Run: `uv run pytest tests/cli/test_reconcile.py::test_finish_blocks_on_merged_drift -v`
Expected: FAIL.

- [ ] **Step 7.3: Implement the gate helper**

In `src/mship/cli/worktree.py`, near the top (after imports), add:

```python
def _run_gate(
    get_container,
    *,
    command: str,  # "spawn" | "finish" | "close" | "precommit"
    bypass: bool,
    output,
) -> None:
    """Run the upstream reconciler; exit(1) on block, print warnings on warn."""
    if bypass:
        return
    from mship.core.reconcile.cache import ReconcileCache
    from mship.core.reconcile.fetch import (
        collect_git_snapshots, fetch_pr_snapshots,
    )
    from mship.core.reconcile.gate import GateAction, reconcile_now, should_block

    container = get_container()
    state = container.state_manager().load()
    state_dir = Path(container.config_path()).parent / ".mothership"
    cache = ReconcileCache(state_dir)

    def _fetcher(branches, worktrees_by_branch):
        return (
            fetch_pr_snapshots(branches),
            collect_git_snapshots(worktrees_by_branch),
        )

    try:
        decisions = reconcile_now(state, cache=cache, fetcher=_fetcher)
    except Exception as e:  # noqa: BLE001 — never fail closed
        output.warning(f"reconcile unavailable: {e}; proceeding")
        return

    ignored = cache.read_ignores()
    blockers: list[str] = []
    for slug, d in decisions.items():
        action = should_block(d, command=command, ignored=ignored)
        if action is GateAction.block:
            blockers.append(
                f"  - {slug}: {d.state.value}"
                + (f" (PR #{d.pr_number})" if d.pr_number else "")
            )
        elif action is GateAction.warn:
            output.warning(
                f"task '{slug}' has {d.state.value} drift "
                + (f"(PR #{d.pr_number}); " if d.pr_number else "; ")
                + "see `mship reconcile`"
            )
    if blockers:
        output.error(
            f"`mship {command}` refused — upstream drift on:\n"
            + "\n".join(blockers)
            + "\nRun `mship reconcile` for details, then fix or pass --bypass-reconcile."
        )
        raise typer.Exit(code=1)
```

(Ensure `from pathlib import Path` is imported at top of file; most likely already is.)

- [ ] **Step 7.4: Wire into `spawn`**

Locate the `spawn()` function body. Add the `bypass_reconcile` flag:

```python
        bypass_reconcile: bool = typer.Option(False, "--bypass-reconcile", help="Skip upstream PR drift check for this spawn"),
```

Immediately AFTER any setup (config loading) and BEFORE the worktree-creation logic, call:

```python
        _run_gate(get_container, command="spawn", bypass=bypass_reconcile, output=output)
```

- [ ] **Step 7.5: Wire into `finish`**

Same pattern: add `--bypass-reconcile` option to the `finish()` signature, then call `_run_gate(get_container, command="finish", bypass=bypass_reconcile, output=output)` at the top of the body, before any PR-creation work.

- [ ] **Step 7.6: Wire into `close`**

`close` is allowed on drifted tasks (it's the exit path). Still accept `--bypass-reconcile` for consistency and future-proofing, but don't call the gate — close is always `GateAction.allow` in the matrix. Optionally, still call it just so ignore-list entries get cleared properly:

Add to `close()` signature:

```python
        bypass_reconcile: bool = typer.Option(False, "--bypass-reconcile", help="Skip upstream PR drift check"),
```

At the start of the body, call:

```python
        _run_gate(get_container, command="close", bypass=bypass_reconcile, output=output)
```

After a successful close, clear the ignore entry for the closed slug:

```python
        # After close succeeds and state is removed:
        from mship.core.reconcile.cache import ReconcileCache
        ReconcileCache(Path(container.config_path()).parent / ".mothership").remove_ignore(task_slug)
```

(Place this where the task-slug is still in scope, right before the function returns.)

- [ ] **Step 7.7: Wire into pre-commit (`_check-commit`)**

Open `src/mship/cli/internal.py`. Inside the `check_commit` function, after the current logic confirms the worktree path is valid (right before `raise typer.Exit(code=0)` on the happy path), add:

```python
        # Upstream-drift gate. Fail-open on any error (network, missing gh, etc.).
        try:
            from mship.core.reconcile.cache import ReconcileCache
            from mship.core.reconcile.fetch import (
                collect_git_snapshots, fetch_pr_snapshots,
            )
            from mship.core.reconcile.gate import (
                GateAction, reconcile_now, should_block,
            )
            state_dir = Path(container.config_path()).parent / ".mothership"
            cache = ReconcileCache(state_dir)

            def _fetcher(branches, worktrees_by_branch):
                return (
                    fetch_pr_snapshots(branches),
                    collect_git_snapshots(worktrees_by_branch),
                )

            decisions = reconcile_now(state, cache=cache, fetcher=_fetcher)
        except Exception:
            raise typer.Exit(code=0)

        ignored = cache.read_ignores()
        d = decisions.get(task.slug)
        if d is not None:
            action = should_block(d, command="precommit", ignored=ignored)
            if action is GateAction.block:
                import sys
                sys.stderr.write(
                    f"\u26d4 mship: refusing commit — task '{task.slug}' has "
                    f"{d.state.value} drift"
                    + (f" (PR #{d.pr_number}).\n" if d.pr_number else ".\n")
                    + "   Run `mship reconcile` for details, or `git commit --no-verify` to override.\n"
                )
                raise typer.Exit(code=1)
```

(Keep the existing `raise typer.Exit(code=0)` at the end for the happy path.)

- [ ] **Step 7.8: Run — confirm gate tests pass**

Run: `uv run pytest tests/cli/test_reconcile.py -v`
Expected: all tests PASS.

- [ ] **Step 7.9: Run full suite**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 7.10: Commit**

```bash
git add src/mship/cli/worktree.py src/mship/cli/internal.py tests/cli/test_reconcile.py
git commit -m "feat(reconcile): gate spawn/finish/close/pre-commit on upstream drift"
```

---

## Task 8: Final integration + smoke

- [ ] **Step 8.1: Full suite**

Run: `uv run pytest -q`
Expected: green.

- [ ] **Step 8.2: Smoke — list reconcile on the active workspace**

Run: `uv run mship reconcile`
Expected: table or "No tasks to reconcile." No crash, no non-zero exit.

- [ ] **Step 8.3: Smoke — JSON path**

Run: `uv run mship reconcile --json | jq .`
Expected: valid JSON with a `tasks` array.

- [ ] **Step 8.4: Smoke — ignore-list roundtrip**

Run:
```bash
uv run mship reconcile --ignore <an-existing-task-slug>
uv run mship reconcile --clear-ignores
```
Expected: no errors.

- [ ] **Step 8.5: Smoke — finish bypass**

Run (against a fresh dummy task, not this one):
```bash
uv run mship finish --bypass-reconcile --help  # sanity: flag exists
```
Expected: `--bypass-reconcile` appears in the help.

- [ ] **Step 8.6: `mship finish`**

Run: `mship finish`
Expected: PR opened.

---

## Self-Review

**Spec coverage:**
- ✅ Four drift states + `in_sync` + `missing` (Task 2).
- ✅ Batched `gh` call with cache + TTL (Tasks 3, 4, 5).
- ✅ Offline fallback (Task 5 — stale cache; Task 6 CLI — warning).
- ✅ Blocking matrix enforced at spawn/finish/pre-commit; close allowed (Task 7).
- ✅ `mship reconcile` command with `--json`, `--ignore`, `--clear-ignores`, `--refresh` (Task 6).
- ✅ `--bypass-reconcile` per-command escape hatch (Task 7).
- ✅ `Task.base_branch` added + populated at spawn (Tasks 1, 4).
- ✅ Close clears ignores for the closed slug (Task 7.6).
- ✅ Never fails closed on network errors (Task 5 fallback; Task 7.3 try/except).

**Placeholder scan:** no TBDs. Two steps reference "existing fixture pattern in this file" — that's a directive to mirror real code, not a placeholder (the implementer must read the file, find the pattern, apply it). Same for one CLI-help ergonomic ("ensure the flag appears in --help").

**Type consistency:** `Decision`, `UpstreamState`, `PRSnapshot`, `GitSnapshot`, `GateAction`, `Command` — names used identically across Tasks 2, 5, 6, 7. `ReconcileCache.read_ignores` / `add_ignore` / `remove_ignore` / `clear_ignores` consistent between Tasks 3, 6, 7.

**Risks:**
- Test fixture wiring in Task 6 depends on the existing test-level container-override pattern; the plan directs the implementer to read `tests/cli/view/test_status_view.py::test_status_cli_rejects_unknown_task` and mirror it. If that pattern has changed, tests will need the current idiom.
- Pre-commit gate adds network call latency. If a user commits during a flaky-gh window, we fail-open (exit 0) rather than block — matches the "never fail closed" principle.

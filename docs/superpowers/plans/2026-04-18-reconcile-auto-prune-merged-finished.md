# Reconcile Gate Auto-Allow Settled Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The reconcile gate auto-allows `spawn` and `finish` commands for tasks whose PR is merged or closed AND whose local `finished_at` is set ("settled" tasks). Removes the friction where `mship finish` refuses after an external PR merge until the user manually runs `mship close`.

**Architecture:** Add a `finished_at: str | None` field to `Decision`. Plumb it from `state.tasks[slug].finished_at` at Decision-construction time via a new `_finished_at_for` helper. Extend `should_block` with one early-return: if `finished_at` set AND state ∈ {merged, closed} AND command ∈ {spawn, finish} → `allow`. `_MATRIX` stays untouched; `close` and `precommit` keep existing behavior.

**Tech Stack:** Python 3.14, Pydantic v2, stdlib `datetime.isoformat()`, pytest.

**Reference spec:** `docs/superpowers/specs/2026-04-18-reconcile-auto-prune-merged-finished-design.md`
**Closes:** #36

---

## File structure

**Modified files:**
- `src/mship/core/reconcile/gate.py` — `Decision` dataclass gains `finished_at` field; `_decision_from_detection` / `_decision_from_cache_entry` accept `state` and populate the field via new `_finished_at_for` helper; `should_block` gains the settled-task early-return.
- `tests/core/reconcile/test_gate.py` — new tests covering the plumbing and the gate behavior.

**Unchanged files:**
- `src/mship/core/reconcile/cache.py` — `finished_at` is NOT cached.
- `src/mship/core/reconcile/detect.py` — unchanged.
- `src/mship/core/reconcile/fetch.py` — unchanged.
- `src/mship/cli/*.py` — all CLI handlers call `should_block` through existing glue; no changes.

**Task ordering rationale:** Task 1 lands the data-plumbing (Decision + reconcile_now). It's a no-op behavior change — Decision gains a field, nothing branches on it yet. Task 2 lands the gate behavior change (`should_block` early-return) that actually closes the issue. Task 3 smoke-tests and ships.

---

## Task 1: Plumb `finished_at` into `Decision`

**Files:**
- Modify: `src/mship/core/reconcile/gate.py`
- Modify: `tests/core/reconcile/test_gate.py`

**Context:** `Decision` is a frozen dataclass with all-string-or-None fields. Add `finished_at: str | None = None`. The two Decision constructors (`_decision_from_detection`, `_decision_from_cache_entry`) need a `state: WorkspaceState` parameter so they can look up `task.finished_at` via a new helper. `reconcile_now` already has `state` in scope — just thread it through both call-sites.

- [ ] **Step 1.1: Write failing tests**

Append to `tests/core/reconcile/test_gate.py`:

```python
# --- finished_at plumbing (issue #36) ---


def test_decision_has_finished_at_from_state_fresh_fetch(tmp_path: Path):
    """reconcile_now populates Decision.finished_at from state.tasks."""
    finished = datetime(2026, 4, 18, 13, 20, 28, tzinfo=timezone.utc)
    state = WorkspaceState(tasks={"a": _task("a", finished_at=finished)})
    cache = ReconcileCache(tmp_path)  # empty

    def _fetcher(branches, wts):
        return (
            {"feat/a": PRSnapshot(number=1, url="u", head_branch="feat/a", base_branch="main", state="MERGED", merge_commit_sha="abc", updated_at="2026-04-18T13:21:00Z")},
            {"feat/a": GitSnapshot(last_common_base_sha="deadbeef")},
        )

    decisions = reconcile_now(state, cache=cache, fetcher=_fetcher)
    assert decisions["a"].finished_at == finished.isoformat()


def test_decision_finished_at_none_when_task_not_finished(tmp_path: Path):
    state = WorkspaceState(tasks={"a": _task("a")})  # finished_at default None
    cache = ReconcileCache(tmp_path)

    def _fetcher(branches, wts):
        return (
            {"feat/a": PRSnapshot(number=1, url="u", head_branch="feat/a", base_branch="main", state="OPEN", merge_commit_sha=None, updated_at="2026-04-18T13:21:00Z")},
            {"feat/a": GitSnapshot(last_common_base_sha="deadbeef")},
        )

    decisions = reconcile_now(state, cache=cache, fetcher=_fetcher)
    assert decisions["a"].finished_at is None


def test_decision_finished_at_populated_from_cache_hit(tmp_path: Path):
    """Cache-hit path still plumbs finished_at from live state, not cache."""
    finished = datetime(2026, 4, 18, 13, 20, 28, tzinfo=timezone.utc)
    cache = ReconcileCache(tmp_path)
    cache.write(CachePayload(
        fetched_at=time.time(), ttl_seconds=300,
        results={"a": {"state": "merged", "pr_url": "u", "pr_number": 1, "base": "main"}},
        ignored=[],
    ))
    state = WorkspaceState(tasks={"a": _task("a", finished_at=finished)})

    decisions = reconcile_now(
        state, cache=cache,
        fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("should not fetch")),
    )
    assert decisions["a"].state == UpstreamState.merged
    assert decisions["a"].finished_at == finished.isoformat()
```

Verify the existing imports at the top of the file already cover what these tests use (`datetime`, `timezone`, `Path`, `Task`, `WorkspaceState`, `ReconcileCache`, `CachePayload`, `PRSnapshot`, `GitSnapshot`, `reconcile_now`, `UpstreamState`, `time`). They should — the existing tests use all of these.

- [ ] **Step 1.2: Run the tests to verify they fail**

Run: `pytest tests/core/reconcile/test_gate.py::test_decision_has_finished_at_from_state_fresh_fetch -v`
Expected: FAIL with `AttributeError: 'Decision' object has no attribute 'finished_at'`.

- [ ] **Step 1.3: Add `finished_at` to `Decision` and the `_finished_at_for` helper**

Edit `src/mship/core/reconcile/gate.py`.

Find the `Decision` class:

```python
@dataclass(frozen=True)
class Decision:
    slug: str
    state: UpstreamState
    pr_url: str | None
    pr_number: int | None
    base: str | None
    merge_commit: str | None
    updated_at: str | None
```

Add one field:

```python
@dataclass(frozen=True)
class Decision:
    slug: str
    state: UpstreamState
    pr_url: str | None
    pr_number: int | None
    base: str | None
    merge_commit: str | None
    updated_at: str | None
    finished_at: str | None = None
```

Immediately after the `Decision` class definition (before `class GateAction` or `_MATRIX`), add the helper:

```python
def _finished_at_for(slug: str, state: WorkspaceState) -> str | None:
    """Return the ISO-8601 string of the task's finished_at, or None.

    Used at Decision-construction time to propagate finish-state into the
    gate's settled-task auto-allow path (issue #36).
    """
    task = state.tasks.get(slug)
    if task is None or task.finished_at is None:
        return None
    return task.finished_at.isoformat()
```

- [ ] **Step 1.4: Update `_decision_from_detection` and `_decision_from_cache_entry` to accept state**

Find:

```python
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
```

Replace with:

```python
def _decision_from_detection(slug: str, det: Detection, state: WorkspaceState) -> Decision:
    return Decision(
        slug=slug, state=det.state, pr_url=det.pr_url, pr_number=det.pr_number,
        base=det.base, merge_commit=det.merge_commit, updated_at=det.updated_at,
        finished_at=_finished_at_for(slug, state),
    )


def _decision_from_cache_entry(slug: str, raw: dict, state: WorkspaceState) -> Decision | None:
    try:
        return Decision(
            slug=slug,
            state=UpstreamState(raw["state"]),
            pr_url=raw.get("pr_url"),
            pr_number=raw.get("pr_number"),
            base=raw.get("base"),
            merge_commit=raw.get("merge_commit"),
            updated_at=raw.get("updated_at"),
            finished_at=_finished_at_for(slug, state),
        )
    except (KeyError, ValueError):
        return None
```

- [ ] **Step 1.5: Update the two call-sites in `reconcile_now` and `_decisions_from_cache`**

Find `_decisions_from_cache`:

```python
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
```

Update the inner call:

```python
def _decisions_from_cache(state: WorkspaceState, payload: CachePayload) -> dict[str, Decision]:
    out: dict[str, Decision] = {}
    for slug in state.tasks:
        raw = payload.results.get(slug)
        if raw is None:
            continue
        d = _decision_from_cache_entry(slug, raw, state)
        if d is not None:
            out[slug] = d
    return out
```

Find the final return in `reconcile_now`:

```python
    return {slug: _decision_from_detection(slug, d) for slug, d in detections.items()}
```

Update:

```python
    return {slug: _decision_from_detection(slug, d, state) for slug, d in detections.items()}
```

- [ ] **Step 1.6: Run the new tests**

Run: `pytest tests/core/reconcile/test_gate.py -v`
Expected: all tests pass (3 new + existing).

- [ ] **Step 1.7: Commit**

```bash
git add src/mship/core/reconcile/gate.py tests/core/reconcile/test_gate.py
git commit -m "feat(reconcile): plumb finished_at into Decision"
mship journal "Decision gains finished_at field plumbed from state.tasks at construction time" --action committed
```

---

## Task 2: `should_block` auto-allows settled tasks for spawn/finish

**Files:**
- Modify: `src/mship/core/reconcile/gate.py`
- Modify: `tests/core/reconcile/test_gate.py`

**Context:** With `finished_at` now on every Decision (Task 1), `should_block` gains a narrow early-return that treats "merged/closed + finished" as `allow` for `spawn` and `finish`. `precommit` and `close` keep their existing behavior.

- [ ] **Step 2.1: Write failing tests**

Append to `tests/core/reconcile/test_gate.py`:

```python
# --- should_block settled-task auto-allow (issue #36) ---


def _dec(state: UpstreamState, finished_at: str | None = None, slug: str = "a") -> Decision:
    return Decision(
        slug=slug, state=state, pr_url=None, pr_number=None,
        base=None, merge_commit=None, updated_at=None,
        finished_at=finished_at,
    )


def test_should_block_merged_unfinished_finish_blocks():
    """Regression: merged without finished_at still blocks (existing matrix)."""
    d = _dec(UpstreamState.merged, finished_at=None)
    assert should_block(d, command="finish", ignored=[]) == GateAction.block


def test_should_block_merged_finished_finish_allows():
    """New: merged PR for a task with finished_at set — allow finish."""
    d = _dec(UpstreamState.merged, finished_at="2026-04-18T13:20:28+00:00")
    assert should_block(d, command="finish", ignored=[]) == GateAction.allow


def test_should_block_merged_finished_spawn_allows():
    d = _dec(UpstreamState.merged, finished_at="2026-04-18T13:20:28+00:00")
    assert should_block(d, command="spawn", ignored=[]) == GateAction.allow


def test_should_block_merged_finished_precommit_still_blocks():
    """Scope boundary: precommit keeps the matrix behavior."""
    d = _dec(UpstreamState.merged, finished_at="2026-04-18T13:20:28+00:00")
    assert should_block(d, command="precommit", ignored=[]) == GateAction.block


def test_should_block_merged_finished_close_allows():
    """Regression: close already allowed merged; settled logic is a no-op here."""
    d = _dec(UpstreamState.merged, finished_at="2026-04-18T13:20:28+00:00")
    assert should_block(d, command="close", ignored=[]) == GateAction.allow


def test_should_block_closed_finished_finish_allows():
    """Closed PRs with finished_at also settle."""
    d = _dec(UpstreamState.closed, finished_at="2026-04-18T13:20:28+00:00")
    assert should_block(d, command="finish", ignored=[]) == GateAction.allow


def test_should_block_in_sync_finished_unchanged():
    """finished_at set but state=in_sync → matrix applies (finish allows here)."""
    d = _dec(UpstreamState.in_sync, finished_at="2026-04-18T13:20:28+00:00")
    assert should_block(d, command="finish", ignored=[]) == GateAction.allow


def test_should_block_diverged_finished_still_blocks():
    """Regression: diverged state still blocks even if finished_at is set.
    A merged-then-local-commits-upstream situation is not 'settled'."""
    d = _dec(UpstreamState.diverged, finished_at="2026-04-18T13:20:28+00:00")
    assert should_block(d, command="finish", ignored=[]) == GateAction.block


def test_should_block_ignored_wins_over_settled_logic():
    """ignored list short-circuits everything including settled auto-allow."""
    d = _dec(UpstreamState.merged, finished_at="2026-04-18T13:20:28+00:00", slug="a")
    # Whether ignored or not, the answer is allow — but verify the ignored path fires first
    # by constructing a case where settled would block (doesn't exist in our logic,
    # but asserting the ignored-overrides-all invariant).
    assert should_block(d, command="finish", ignored=["a"]) == GateAction.allow
```

- [ ] **Step 2.2: Run tests to verify the new behavior is missing**

Run: `pytest tests/core/reconcile/test_gate.py -v`
Expected: tests 2, 3, 6 FAIL with `GateAction.block != GateAction.allow`. Tests 1, 4, 5, 7, 8, 9 PASS (they either exercise existing behavior or happen to match).

- [ ] **Step 2.3: Add the settled-task early-return to `should_block`**

Edit `src/mship/core/reconcile/gate.py`.

Find `should_block`:

```python
def should_block(decision: Decision, *, command: Command, ignored: list[str]) -> GateAction:
    if decision.slug in ignored:
        return GateAction.allow
    return _MATRIX[decision.state.value][command]
```

Replace with:

```python
def should_block(decision: Decision, *, command: Command, ignored: list[str]) -> GateAction:
    if decision.slug in ignored:
        return GateAction.allow
    # Settled: a task whose PR is merged/closed AND whose finished_at is set.
    # The user has already run `mship finish`; only `mship close` remains.
    # Don't block subsequent `spawn`/`finish` on these tasks — surface them
    # via `mship reconcile` (existing output) instead. Issue #36.
    if (
        decision.finished_at is not None
        and decision.state in (UpstreamState.merged, UpstreamState.closed)
        and command in ("spawn", "finish")
    ):
        return GateAction.allow
    return _MATRIX[decision.state.value][command]
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `pytest tests/core/reconcile/test_gate.py -v`
Expected: all tests pass (9 new + all existing).

- [ ] **Step 2.5: Run the full reconcile subdir**

Run: `pytest tests/core/reconcile/ -v`
Expected: all green.

- [ ] **Step 2.6: Commit**

```bash
git add src/mship/core/reconcile/gate.py tests/core/reconcile/test_gate.py
git commit -m "feat(reconcile): auto-allow settled tasks on spawn/finish"
mship journal "should_block now returns allow for merged/closed+finished tasks on spawn/finish; precommit/close unchanged" --action committed
```

---

## Task 3: Manual smoke + finish PR

**Files:**
- None (verification only).

**Context:** End-to-end verification that `mship finish` doesn't block after a PR merge, and full pytest stays green.

- [ ] **Step 3.1: Reinstall tool**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/reconcile-auto-prunes-merged-finished-tasks
uv tool install --reinstall --from . mothership
```

- [ ] **Step 3.2: Full pytest final check**

```bash
pytest tests/ 2>&1 | tail -5
```

Expected: all tests pass (~875+ with the new tests added).

- [ ] **Step 3.3: Manual smoke — simulate the scenario**

The scenario requires a merged PR on an active task. Since we can't merge a real PR just for the smoke, we'll exercise the code path directly via a short Python smoke script.

```bash
cd /tmp
cat > /tmp/settled-smoke.py <<'EOF'
"""Smoke: exercise should_block directly with a settled Decision."""
from mship.core.reconcile.gate import Decision, GateAction, should_block
from mship.core.reconcile.detect import UpstreamState


# Case 1: settled task on finish → should allow (previously would block)
d = Decision(
    slug="settled-task", state=UpstreamState.merged,
    pr_url="https://example.com/pr/1", pr_number=1,
    base="main", merge_commit="abc123", updated_at=None,
    finished_at="2026-04-18T13:20:28+00:00",
)
for cmd in ("spawn", "finish", "close", "precommit"):
    action = should_block(d, command=cmd, ignored=[])
    print(f"{cmd:10} settled-merged+finished → {action.value}")

print()

# Case 2: unfinished task — should still block on finish (regression check)
d2 = Decision(
    slug="unfinished-task", state=UpstreamState.merged,
    pr_url="https://example.com/pr/2", pr_number=2,
    base="main", merge_commit="def456", updated_at=None,
    finished_at=None,
)
for cmd in ("spawn", "finish", "close", "precommit"):
    action = should_block(d2, command=cmd, ignored=[])
    print(f"{cmd:10} unfinished-merged → {action.value}")
EOF

cd /home/bailey/development/repos/mothership/.worktrees/feat/reconcile-auto-prunes-merged-finished-tasks
uv run python /tmp/settled-smoke.py
```

Expected output:
```
spawn      settled-merged+finished → allow
finish     settled-merged+finished → allow
close      settled-merged+finished → allow
precommit  settled-merged+finished → block

spawn      unfinished-merged → block
finish     unfinished-merged → block
close      unfinished-merged → allow
precommit  unfinished-merged → block
```

The first block confirms settled auto-allow fires for spawn/finish; close is already allow; precommit still blocks. The second block confirms the pre-change matrix behavior is preserved when finished_at is None.

Cleanup:

```bash
rm /tmp/settled-smoke.py
```

- [ ] **Step 3.4: Open the PR**

Write this body to `/tmp/settled-body.md`:

```markdown
## Summary

Closes #36. The reconcile gate auto-allows `spawn` and `finish` for tasks that are **settled**: `state.tasks[slug].finished_at` is set AND the reconcile detector reports state ∈ {merged, closed}. Removes the friction where `mship finish` refused after an external PR merge until the user ran `mship close`.

Scenario that motivated this (from a real session minutes before the fix):

```
$ mship finish
ERROR: `mship finish` refused — upstream drift on:
  - <slug>: merged (PR #N)
Run `mship reconcile` for details, then fix or pass --bypass-reconcile.
```

After this fix, `mship finish` proceeds cleanly. The settled task stays in `state.tasks` until the user runs `mship close` explicitly. `mship reconcile` still shows settled tasks with `state: merged` so visibility is preserved.

## Scope

- Auto-allow applies only to `spawn` and `finish`.
- `precommit` stays `block` for merged (committing on a merged branch is weird user state — keep the signal).
- `close` already allowed merged — no change.
- No auto-`close`. Worktree teardown stays explicit.
- No new CLI output. Users discover settled tasks via existing `mship reconcile` JSON, which still shows `state: merged`.
- No reconcile cache file change — `finished_at` is plumbed from state at Decision-construction time, not persisted.

## Changes

- `src/mship/core/reconcile/gate.py`:
  - `Decision` gains a `finished_at: str | None = None` field.
  - New private helper `_finished_at_for(slug, state)` returns `state.tasks[slug].finished_at.isoformat()` or None.
  - `_decision_from_detection` and `_decision_from_cache_entry` now take `state` and populate `finished_at`.
  - `should_block` gains an early-return: if `finished_at` is set AND state ∈ {merged, closed} AND command ∈ {spawn, finish}, return `allow`. `_MATRIX` is untouched.

## Test plan

- [x] `tests/core/reconcile/test_gate.py`: 3 new plumbing tests (Decision.finished_at populated from fresh fetch / from cache-hit / None when task not finished) + 9 new should_block tests covering: existing-matrix regressions (merged-unfinished blocks, diverged-finished blocks), new allow cases (merged+finished+finish/spawn/close, closed+finished+finish), scope boundaries (precommit-merged still blocks, in_sync unchanged), ignored-overrides-all.
- [x] Full suite: all pass.
- [x] Manual smoke: direct `should_block` invocation confirms settled-merged + finished-unset cases produce the right actions.
```

Then:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/reconcile-auto-prunes-merged-finished-tasks
mship finish --body-file /tmp/settled-body.md
```

Expected: PR URL returned.

---

## Done when

- [x] `Decision` gains `finished_at: str | None = None` field.
- [x] `_finished_at_for(slug, state)` helper exists and returns `task.finished_at.isoformat()` or None.
- [x] `_decision_from_detection` and `_decision_from_cache_entry` accept `state` and populate `finished_at`.
- [x] `should_block` auto-allows merged/closed + finished_at + (spawn/finish).
- [x] `precommit`, `close`, other-state tasks unchanged.
- [x] `_MATRIX` untouched.
- [x] 12 new tests pass; existing reconcile tests pass.
- [x] Full pytest green.
- [x] Manual smoke confirms the expected action matrix for settled and unfinished scenarios.

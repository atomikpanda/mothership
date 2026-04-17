# Audit Severity Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-04-17-audit-severity-split-design.md`

**Goal:** Stop blocking spawn/finish on untracked-only dirt while keeping the gate's teeth for tracked-modified dirt.

**Architecture:** Extend `Severity = Literal["error", "warn", "info"]`. Rewrite `_probe_dirty` in `src/mship/core/repo_state.py` to return `tuple[Issue, ...]`, classifying lines from `git status --porcelain` as `dirty_worktree` (error, tracked-modified) or `dirty_untracked` (warn, untracked-only). Add a yellow ⚠ display lane in `src/mship/cli/audit.py`. Existing `has_errors` gate logic stays unchanged — by construction, warn issues never trip it.

**Tech Stack:** Python 3.14, pytest, dataclasses.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `src/mship/core/repo_state.py` | Extend `Severity` literal; rewrite `_probe_dirty` to classify and return tuple; update caller `_audit_one` to splice tuple into issues | modify |
| `src/mship/cli/audit.py` | Add `[yellow]⚠[/yellow]` display lane and `warn(s)` footer counter | modify |
| `tests/core/test_repo_state.py` | New unit tests for classification (untracked-only, modified-only, mixed); migrate existing untracked-fixture tests | modify |
| `tests/cli/test_audit.py` | Migrate existing untracked-fixture tests; add yellow-lane display test | modify |
| `tests/cli/test_sync.py` | Migrate untracked-fixture test (`test_sync_dirty_nonzero`) | modify |
| `tests/core/test_audit_gate.py` | Add gate-behavior test: warn-only audit doesn't block | modify |
| `tests/test_integration.py`, `tests/test_finish_integration.py` | Migrate untracked-fixture assertions | modify |

---

## Task 1: Extend Severity literal and rewrite the dirty probe (TDD)

**Files:**
- Modify: `src/mship/core/repo_state.py`
- Test: `tests/core/test_repo_state.py`

- [ ] **Step 1.1: Write failing tests for the new probe behavior**

Append to `tests/core/test_repo_state.py`:

```python
# --- _probe_dirty classification (issue #35) ---

def test_probe_dirty_untracked_only_emits_warn(audit_workspace):
    cfg, shell = _load(audit_workspace)
    (audit_workspace / "cli" / "new.txt").write_text("hi\n")  # untracked
    rep = audit_repos(cfg, shell, names=["cli"])
    cli = next(r for r in rep.repos if r.name == "cli")
    codes = {(i.code, i.severity) for i in cli.issues}
    assert ("dirty_untracked", "warn") in codes
    assert not any(c == "dirty_worktree" for c, _ in codes)
    assert cli.has_errors is False


def test_probe_dirty_modified_tracked_emits_error(audit_workspace):
    cfg, shell = _load(audit_workspace)
    # README.md is a tracked file in the audit_workspace fixture
    (audit_workspace / "cli" / "README.md").write_text("modified content\n")
    rep = audit_repos(cfg, shell, names=["cli"])
    cli = next(r for r in rep.repos if r.name == "cli")
    codes = {(i.code, i.severity) for i in cli.issues}
    assert ("dirty_worktree", "error") in codes
    assert cli.has_errors is True


def test_probe_dirty_mixed_emits_both(audit_workspace):
    cfg, shell = _load(audit_workspace)
    (audit_workspace / "cli" / "README.md").write_text("modified\n")  # tracked-modified
    (audit_workspace / "cli" / "new.txt").write_text("hi\n")          # untracked
    rep = audit_repos(cfg, shell, names=["cli"])
    cli = next(r for r in rep.repos if r.name == "cli")
    codes = {(i.code, i.severity) for i in cli.issues}
    assert ("dirty_worktree", "error") in codes
    assert ("dirty_untracked", "warn") in codes
    assert cli.has_errors is True
```

Run: `uv run pytest tests/core/test_repo_state.py -k probe_dirty -v`
Expected: 3 FAIL — `dirty_untracked` doesn't exist yet; everything currently fires as `dirty_worktree` error.

- [ ] **Step 1.2: Extend the Severity literal and rewrite `_probe_dirty`**

In `src/mship/core/repo_state.py`, change line 9:

```python
Severity = Literal["error", "warn", "info"]
```

Replace the `_probe_dirty` function (currently at lines 241-261) with:

```python
def _probe_dirty(
    shell,
    root_path: Path,
    subdir: Path | None,
    allow_dirty: bool,
) -> tuple[Issue, ...]:
    if allow_dirty:
        return ()
    cmd = "git status --porcelain"
    if subdir is not None:
        cmd += f" -- {shlex.quote(str(subdir))}"
    rc, out, _ = _sh_out(shell, cmd, root_path)
    if rc != 0:
        return ()
    untracked = 0
    modified = 0
    for line in out.splitlines():
        if not line.strip():
            continue
        # Porcelain v1: first 2 chars are the status code. "??" is untracked;
        # anything else (M, A, D, R, C, U, plus staged/unstaged combos) is
        # tracked-modified content.
        if line.startswith("??"):
            untracked += 1
        else:
            modified += 1
    issues: list[Issue] = []
    if modified:
        issues.append(Issue(
            "dirty_worktree", "error",
            f"{modified} modified tracked file" + ("s" if modified != 1 else ""),
        ))
    if untracked:
        issues.append(Issue(
            "dirty_untracked", "warn",
            f"{untracked} untracked file" + ("s" if untracked != 1 else ""),
        ))
    return tuple(issues)
```

- [ ] **Step 1.3: Update `_audit_one` (the only caller) to splice the tuple**

Find the call site:

```bash
grep -n "_probe_dirty" src/mship/core/repo_state.py
```

Replace the conditional-append (`if dirty_issue: issues.append(dirty_issue)`) with `issues.extend(...)`:

```python
issues.extend(_probe_dirty(shell, root_path, subdir, allow_dirty))
```

- [ ] **Step 1.4: Run the new probe tests**

Run: `uv run pytest tests/core/test_repo_state.py -k probe_dirty -v`
Expected: 3 PASS

- [ ] **Step 1.5: Commit**

```bash
git add src/mship/core/repo_state.py tests/core/test_repo_state.py
git commit -m "feat(audit): split dirty_worktree into modified (error) + untracked (warn)"
```

---

## Task 2: Add yellow ⚠ display lane to `mship audit`

**Files:**
- Modify: `src/mship/cli/audit.py`
- Test: `tests/cli/test_audit.py`

- [ ] **Step 2.1: Write failing test for the warn display**

Append to `tests/cli/test_audit.py`:

```python
def test_audit_warn_displays_yellow_lane(audit_workspace):
    _override(audit_workspace)
    try:
        (audit_workspace / "cli" / "new.txt").write_text("hi\n")  # untracked → warn
        result = runner.invoke(app, ["audit"])
        assert result.exit_code == 0, result.output  # warn does NOT block
        assert "dirty_untracked" in result.output
        assert "warn(s)" in result.output  # footer counter includes warn
    finally:
        _reset()
```

Run: `uv run pytest tests/cli/test_audit.py::test_audit_warn_displays_yellow_lane -v`
Expected: FAIL — the CLI doesn't render a warn lane yet, and exit code will be 1 (current behavior treats untracked as error).

- [ ] **Step 2.2: Add the warn lane in `src/mship/cli/audit.py`**

Replace the existing for-loop body (currently lines 53-59) with:

```python
                for i in r.issues:
                    if i.severity == "error":
                        err_count += 1
                        output.print(f"  [red]✗[/red] {i.code}: {i.message}")
                    elif i.severity == "warn":
                        warn_count += 1
                        output.print(f"  [yellow]⚠[/yellow] {i.code}: {i.message}")
                    else:
                        info_count += 1
                        output.print(f"  [blue]ⓘ[/blue] {i.code}: {i.message}")
```

Initialize `warn_count = 0` near the existing `err_count = 0` and `info_count = 0` (currently lines 45-46). Update the footer (currently line 61):

```python
        output.print(f"{err_count} error(s), {warn_count} warn(s), {info_count} info across {len(report.repos)} repos")
```

- [ ] **Step 2.3: Run the new display test**

Run: `uv run pytest tests/cli/test_audit.py::test_audit_warn_displays_yellow_lane -v`
Expected: PASS

- [ ] **Step 2.4: Commit**

```bash
git add src/mship/cli/audit.py tests/cli/test_audit.py
git commit -m "feat(audit): yellow warn display lane for dirty_untracked"
```

---

## Task 3: Migrate existing tests that asserted untracked-as-error

**Files:**
- Modify: `tests/core/test_repo_state.py`, `tests/cli/test_audit.py`, `tests/cli/test_sync.py`, `tests/test_integration.py`, `tests/test_finish_integration.py`

These tests pre-date the split and use a fixture that creates `new.txt` (untracked). Under the new semantics, untracked → warn, not error. Each one needs either (a) test setup updated to modify a tracked file (when the intent is "dirty blocks"), or (b) assertions updated to expect warn-not-error semantics (when the intent is "dirty surfaces").

- [ ] **Step 3.1: Migrate `tests/core/test_repo_state.py::test_audit_dirty_worktree`**

This test creates an untracked file and asserts `dirty_worktree`. Intent: verify the probe surfaces dirt. Best migration: switch the setup to modify a tracked file so the assertion (and the test name) stay valid:

Replace lines 169-173 with:

```python
def test_audit_dirty_worktree(audit_workspace):
    cfg, shell = _load(audit_workspace)
    (audit_workspace / "cli" / "README.md").write_text("modified\n")  # tracked-modified
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "dirty_worktree" in _issue_codes(rep, "cli")
```

- [ ] **Step 3.2: Migrate `tests/core/test_repo_state.py::test_audit_allow_dirty_suppresses`**

Replace the setup line (currently line 182):

```python
    (audit_workspace / "cli" / "README.md").write_text("modified\n")
```

- [ ] **Step 3.3: Migrate `tests/core/test_repo_state.py` lines 444 and 519-520**

Inspect those tests with:

```bash
sed -n '430,450p;510,525p' tests/core/test_repo_state.py
```

For each test that creates an untracked file (`.write_text(...)` of a *new* path) and asserts `"dirty_worktree" in codes`, replace the setup to modify a tracked file (e.g., the existing `README.md` in each repo) so the existing assertion holds. If a test's intent is "untracked-only behavior" rather than "tracked-dirty surfaces," update the assertion to `dirty_untracked` instead.

- [ ] **Step 3.4: Migrate `tests/cli/test_audit.py::test_audit_dirty_exits_one`**

Replace lines 31-39 with:

```python
def test_audit_modified_tracked_exits_one(audit_workspace):
    _override(audit_workspace)
    try:
        (audit_workspace / "cli" / "README.md").write_text("modified\n")  # tracked-modified
        result = runner.invoke(app, ["audit"])
        assert result.exit_code == 1
        assert "dirty_worktree" in result.output
    finally:
        _reset()
```

- [ ] **Step 3.5: Migrate `tests/cli/test_audit.py::test_audit_json_shape`**

Replace the setup line (currently line 45):

```python
        (audit_workspace / "cli" / "README.md").write_text("modified\n")
```

- [ ] **Step 3.6: Migrate `tests/cli/test_sync.py::test_sync_dirty_nonzero`**

Currently (lines 21-32) creates untracked file and expects exit 1. Replace setup line (currently line 25):

```python
        (audit_workspace / "cli" / "README.md").write_text("modified\n")
```

- [ ] **Step 3.7: Migrate `tests/test_integration.py:121` and `tests/test_finish_integration.py:309, 551`**

For each, find the `.write_text(...)` setup line that creates the dirty condition. If it creates a new path (untracked), change the path to an existing tracked file (`README.md` or similar). Confirm with:

```bash
grep -n "write_text" tests/test_integration.py | head
grep -n "write_text" tests/test_finish_integration.py | head
```

The fix per occurrence: switch the path to an existing tracked file in the same repo.

- [ ] **Step 3.8: Run all migrated test files**

```bash
uv run pytest tests/core/test_repo_state.py tests/cli/test_audit.py tests/cli/test_sync.py tests/test_integration.py tests/test_finish_integration.py -v
```

Expected: all PASS. If any fail, the migration in that test missed a setup detail — re-read it and fix.

- [ ] **Step 3.9: Commit**

```bash
git add tests/
git commit -m "test(audit): migrate untracked-fixture tests to use tracked-modified setup"
```

---

## Task 4: Gate-behavior regression test

**Files:**
- Modify: `tests/core/test_audit_gate.py`

- [ ] **Step 4.1: Write a test confirming warn-only audit doesn't block**

Append to `tests/core/test_audit_gate.py`:

```python
def test_gate_does_not_block_when_only_warn_issues():
    """A repo audit with only `dirty_untracked` (warn) must not trip the gate."""
    from mship.core.repo_state import RepoAudit, AuditReport, Issue
    from pathlib import Path

    audit = RepoAudit(
        name="cli", path=Path("/abs"), current_branch="main",
        issues=(Issue("dirty_untracked", "warn", "1 untracked file"),),
    )
    report = AuditReport(repos=(audit,))
    assert report.has_errors is False
```

Run: `uv run pytest tests/core/test_audit_gate.py::test_gate_does_not_block_when_only_warn_issues -v`
Expected: PASS (no implementation change — this is a regression guard)

- [ ] **Step 4.2: Commit**

```bash
git add tests/core/test_audit_gate.py
git commit -m "test(audit_gate): regression — warn-only audit doesn't block"
```

---

## Task 5: Full verification + PR

- [ ] **Step 5.1: Full test suite**

```bash
uv run pytest -x -q
```

Expected: all pass. If any test outside the migrated set fails, it's likely also using the untracked-fixture pattern — search and migrate per Task 3.

- [ ] **Step 5.2: Spec coverage check**

Each spec section ↔ task:
- ✅ Type extension (`Severity` literal) — Task 1.2
- ✅ Probe split returning tuple — Task 1.2
- ✅ Two-issue emission for mixed dirt — Task 1.1 / 1.2
- ✅ CLI yellow ⚠ lane + footer counter — Task 2
- ✅ Gate behavior unchanged (`has_errors` not tripped by warn) — Tasks 1.1, 4.1
- ✅ Test surface migration — Task 3
- ✅ JSON schema absorbs `"warn"` without version bump — Task 1.2 (no schema change needed; Task 3.5 verifies via `test_audit_json_shape` migration)

- [ ] **Step 5.3: Open the PR via `mship finish` with a real body**

```bash
mship finish --body-file - <<'EOF'
## Summary

- Split `dirty_worktree` into `dirty_worktree` (error, modified-tracked) + `dirty_untracked` (warn, untracked).
- Added `"warn"` to the `Severity` literal (matches `CheckResult.status` convention).
- `mship audit` gains a yellow ⚠ display lane.
- Spawn/finish gate (`has_errors`) unchanged — warn issues don't block.

Closes #35.

## Test plan

- [x] New unit tests: untracked-only → warn, modified-only → error, mixed → both.
- [x] CLI display: warn lane prints yellow ⚠ and the footer counter shows `warn(s)`.
- [x] Gate regression: warn-only audit does NOT trip `has_errors`.
- [x] Migrated all pre-existing tests that used the untracked-fixture pattern.
- [x] Full pytest green.
EOF
```

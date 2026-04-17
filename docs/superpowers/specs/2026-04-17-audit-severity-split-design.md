# Audit severity split — Design

## Context

`mship audit` today emits a single `dirty_worktree` issue at severity `error` whenever `git status --porcelain` produces any output. Untracked files (e.g. `.claude/`, `.codex/`, editor swaps) trip this gate just as readily as modified tracked files, even though the two have very different consequences:

- **Untracked new files** (`??` in porcelain): typically tool/IDE state. Don't affect what's on the branch; don't block any subsequent operation.
- **Modified tracked files**: real uncommitted work that could be lost if the user later resets, switches branches, or runs an `--abandon`.

Because `mship spawn` / `finish` block on any audit error, users hit `--force-audit` reflexively for the untracked case, training them to ignore the gate altogether. That defeats the gate for the case it actually exists to protect.

This is GitHub issue #35.

## Goal

Stop blocking spawn/finish on untracked-only dirt. Keep the gate's teeth for tracked-modified dirt.

## Non-goals

- Workspace-config `audit.ignore_paths` list (issue #35 Option B). Composes cleanly with this fix later but adds config surface that's not needed to address the reported pain.
- Restructuring the audit issue taxonomy beyond adding one new code.

## Architecture

### Type extension

```python
# src/mship/core/repo_state.py
Severity = Literal["error", "warn", "info"]   # was: ["error", "info"]
```

`"warn"` is already the canonical third-tier name in this codebase — `CheckResult.status` in `core/doctor.py` uses `"pass" | "warn" | "fail"`. No new vocabulary.

### Probe split

`_probe_dirty()` returns `tuple[Issue, ...]` instead of `Issue | None`. Walks each line of `git status --porcelain`, classifies by the leading two-character status, and emits up to two issues:

| Porcelain prefix | Class | Issue |
|---|---|---|
| `??` | untracked | `Issue("dirty_untracked", "warn", "<n> untracked file(s)")` |
| anything else (`M `, ` M`, `A `, `MM`, `D `, `R `, `C `, `U `, ...) | modified-tracked | `Issue("dirty_worktree", "error", "<n> modified tracked file(s)")` |

When both classes of dirt exist, both issues fire. `Issue` ordering: error first, then warn (purely cosmetic for human display; JSON consumers shouldn't depend on order).

### Gate behavior — unchanged by design

- `RepoAudit.has_errors` keeps its `severity == "error"` check (already correct: warn issues don't trip it).
- `AuditReport.has_errors` aggregates across repos via `has_errors` — same.
- `audit_gate.py` blocks spawn/finish on `has_errors`. Warn flows through to user output but doesn't block.
- `--bypass-audit` (and the existing `--force-audit` alias) still work for the rare case where modified-tracked content legitimately needs an override.

### CLI display

`src/mship/cli/audit.py` adds a yellow ⚠ lane between the existing red ✗ (error) and blue ⓘ (info):

```
  [yellow]⚠[/yellow] {code}: {message}
```

Footer counter becomes `"{err} error(s), {warn} warn(s), {info} info across N repos"`.

### Output schemas

JSON output (`mship audit --json`) carries the `severity` field per issue, so the existing schema accommodates `"warn"` without a version bump. Consumers that filtered on `severity == "error"` keep their current semantics.

## Testing

In `tests/core/test_repo_state.py` (new cases):

- **Untracked-only dirt** → `_probe_dirty` returns one `Issue("dirty_untracked", "warn", ...)`; `has_errors == False`.
- **Modified-only dirt** → returns one `Issue("dirty_worktree", "error", ...)`; `has_errors == True`.
- **Mixed dirt** (one modified, one untracked) → returns both issues; `has_errors == True`.
- **Clean** → returns empty tuple.

In `tests/cli/test_audit.py` (revisions):

- Any existing test that asserted "untracked file produces a `dirty_worktree` error" gets re-targeted to assert `dirty_untracked` warn.
- Add a test confirming the yellow ⚠ display lane fires for warn issues.

In `tests/core/test_audit_gate.py`:

- Confirm a repo audit with only `dirty_untracked` does NOT block spawn/finish (gate sees `has_errors == False`).
- Confirm a repo audit with `dirty_worktree` DOES block (regression guard on the unchanged gate behavior).

## Migration / compatibility

- **No state migration.** No on-disk format changes.
- **JSON consumers** that switch on `code == "dirty_worktree"` keep working — that code still exists for the modified case. Consumers gain an additional `code == "dirty_untracked"` warn-tier signal.
- **Workflow muscle memory:** users who reach for `--force-audit` to clear untracked-only dirt will find they no longer need it. The flag itself is unchanged.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | Two distinct issue codes (`dirty_worktree` error + `dirty_untracked` warn) instead of one code with conditional severity | Agents reading audit JSON can distinguish without parsing the `message` string; mirrors how `audit` already separates `behind_remote`, `diverged`, etc. into distinct codes. |
| 2 | `"warn"` (not `"warning"`) for the new severity | Matches the existing `CheckResult.status` convention in `core/doctor.py`. |
| 3 | `_probe_dirty` returns a tuple, not a list of one optional issue | Lets the mixed-dirt case emit both issues atomically without caller-side stitching. |
| 4 | Defer Option B (config-level `audit.ignore_paths`) | Severity split alone resolves the reported pain (untracked-only no longer blocks). The config approach is additive and worth its own design decision. |

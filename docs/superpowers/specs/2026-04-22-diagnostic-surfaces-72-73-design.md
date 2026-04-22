# Diagnostic Surfaces: Symlink-Ignore Warn + PR State Reason — Design

Closes #72 and #73.

## Problem

Two adjacent diagnostic gaps where mship knows the signal but doesn't surface it clearly:

1. **#72 — `symlink_dirs` + `.gitignore` footgun.** If `.gitignore` has `foo/` (directory form) but `symlink_dirs: [foo]` creates a **symlink** named `foo` in the worktree, git treats the symlink as a file, not a dir. `foo/` doesn't match — symlink shows as untracked. Audit/finish/close flag the worktree as dirty. The fix (add both `foo` and `foo/` to `.gitignore`) is tribal knowledge.

2. **#73 — `mship close` opaque "pr state unknown".** When `gh pr view` fails, `check_pr_state` returns the string `"unknown"` and `close` logs `closed: pr state unknown` with no context. Users can't tell if it's auth, network, rate limit, 404, or an unmapped state string.

## Solution

Both are pure additive fixes to existing diagnostic code paths:

1. **#72 — Add a `git check-ignore` probe** to spawn's symlink creation and to doctor's repo-by-repo checks. When the literal symlink name (not the dir form) is not ignored, warn with the exact fix.

2. **#73 — `check_pr_state` returns a `(state, reason)` pair.** `close`'s log message surfaces the reason when state is unknown. The reason is classified from gh's stderr/exit-code signals.

Two logically independent fixes, one PR (coherent "better diagnostic surfaces" theme, both small).

## Scope

### #72 scope
- New helper: probe two `git check-ignore` calls per symlink to detect the exact footgun:
  1. `git check-ignore <name>/` (with trailing slash) — does the user's `.gitignore` intend to ignore the directory?
  2. `git check-ignore <name>` (no trailing slash) — does it actually ignore the symlink (which git sees as a file)?
- Warn **only when (1) is yes AND (2) is no**. That's the "dir-form-only ignore missed the symlink" case. Don't warn on symlinks whose originals weren't ignored at all — those are legitimate use cases where the user wants the linked content tracked.
- Spawn: after each `target.symlink_to(...)` in `_create_symlinks`, add a non-fatal warning to the returned list when the footgun fires.
- Doctor: new check per repo with `symlink_dirs` — iterate, probe each name, emit one `warn` row per footgun hit.
- Out of scope: auto-fixing the `.gitignore`. Out of scope: global workspace `.gitignore` — scope is per-repo. Out of scope: warning on symlinks whose originals aren't ignored (legitimate tracked-symlink case).

### #73 scope
- `check_pr_state` signature changes from `(pr_url) -> str` to `(pr_url) -> PrStateResult` NamedTuple with `state: str` (existing values: `merged` / `closed` / `open` / `unknown`) plus `reason: str` (empty for known states; populated for `unknown`).
- Reason classification via substring match on `gh pr view` stderr (+ exit code):
  - exit code 127 → `"gh not installed"` (belt-and-braces; caller usually short-circuits).
  - stderr contains `"rate limit"` (case-insensitive) → `"rate limited"`.
  - stderr contains `"authentication"` / `"not logged in"` / `"gh auth login"` → `"gh not authenticated"`.
  - stderr contains `"could not resolve host"` / `"network is unreachable"` / `"connection timed out"` → `"network error"`.
  - stderr contains `"not found"` / `"could not find pull request"` / `"HTTP 404"` → `"not found"`.
  - returncode == 0 and state string not in mapping → `"unmapped state: <raw>"`.
  - Everything else → `"other: <stderr excerpt>"` (first 80 chars of stderr, stripped).
- Close's log message becomes `f"closed: pr state unknown ({reason})"` when any PR is unknown. If multiple PRs have different unknown reasons, pick the first (single-repo tasks are the common case; for multi-repo, one concrete hint is still better than `unknown`).
- Callers: only `src/mship/cli/worktree.py:471` consumes `check_pr_state` today. Update to handle the tuple.

## Architecture

Two small changes, each isolated to one subsystem. No shared helpers — the checks operate on different data.

```
mship.core.worktree._symlink_gitignore_footgun(repo_path, name) -> bool
  └─ True when `name/` is ignored but `name` is not (the footgun case)

mship.core.worktree._create_symlinks()
  └─ probes the helper per symlink; appends warning to return list on hit

mship.core.doctor.DoctorChecker.run()
  └─ new loop over repos with symlink_dirs
     └─ same helper, appends warn CheckResult per hit

mship.core.pr.PRManager.check_pr_state() -> PrStateResult (NamedTuple)
  └─ classifies reason from subprocess result

mship.cli.worktree.close() (line ~471)
  └─ pr_states = [pr_mgr.check_pr_state(url) for url in ...]
  └─ routes on .state; uses .reason for "pr state unknown" log msg
```

## Resolver change (#73)

```python
from typing import NamedTuple


class PrStateResult(NamedTuple):
    state: str       # "merged" | "closed" | "open" | "unknown"
    reason: str      # "" when known; classified label when unknown
```

Return tuple is chosen over adding `state` + `reason` as separate return values to keep the single-line caller pattern. `NamedTuple` is backward-compatible with iterable unpacking (`state, reason = pr_mgr.check_pr_state(url)`).

## Classification (#73)

Tested signatures (from real `gh` stderr):

- `GraphQL: API rate limit exceeded for user ID …` → `rate limited`
- `authentication required; run 'gh auth login'` → `gh not authenticated`
- `could not resolve host: api.github.com` → `network error`
- `could not find pull request` / `GraphQL: Could not resolve to a PullRequest` → `not found`
- `http: api.github.com/... 403` / other 4xx/5xx → `other: HTTP 403`

The spec intentionally does NOT try to be exhaustive — a simple substring match against a handful of real signatures covers the common cases; everything else falls into `other: <80-char excerpt>` which still names something actionable.

## Spawn warning (#72)

```
{repo_name}: symlink '{name}' is not ignored — git treats it as an untracked file.
Add '{name}' (not just '{name}/') to .gitignore.
```

Single line. Matches the existing symlink-warning style (`{repo}: symlink source missing: {name}` etc.) for consistency.

## Doctor row (#72)

```
{repo}/symlink-ignore   warn   symlink '{name}' is not ignored — add '{name}' (no trailing slash) to .gitignore
```

One row per bad entry. `status=warn` (not `fail`) — consistent with other doctor-detected footguns.

## Testing

### #72 tests (`tests/core/test_worktree.py` + `tests/core/test_doctor.py`)

Truth table the helper must satisfy (returns True = footgun fires = warn):

| `.gitignore` contains | `name/` ignored? | `name` ignored? | Footgun? |
|---|---|---|---|
| `name/` (dir form only) | yes | no | **yes** — warn |
| `name` (no slash) | yes | yes | no |
| both `name` and `name/` | yes | yes | no |
| neither | no | no | no (legitimate tracked symlink) |

- Unit: `_symlink_gitignore_footgun(repo_path, name)` returns True only for the dir-form-only row.
- Spawn integration: spawn a workspace where `.gitignore` has `foo/` and `symlink_dirs: [foo]`; assert spawn output includes the warning.
- Doctor integration: same setup, assert doctor report has a warn row with the expected name.
- Regression: spawn where `.gitignore` has `foo` (no trailing slash) emits NO warning. Spawn where neither `foo` nor `foo/` is ignored emits NO warning (legitimate tracked case).

### #73 tests (`tests/core/test_pr.py` + `tests/cli/test_worktree.py`)

- Unit: `check_pr_state` returns `PrStateResult(state="merged", reason="")` on happy path.
- Classification — parametrized: stderr → reason for each of the 6 signatures.
- Close integration: mock `gh pr view` to fail with "GraphQL: API rate limit" stderr; run `mship close`; assert log message includes `(rate limited)`.
- Regression: known states still return `reason=""` and existing `close` log messages don't regress.

## Anti-goals

- No auto-fixing `.gitignore`. Users copy the warning into their gitignore manually.
- No exhaustive gh error taxonomy. 6 signatures + "other" is good enough.
- No change to `mship audit` or other callers — `check_pr_state` is only consumed by `close` today.
- No global "all diagnostic commands return structured reasons" refactor. Just `check_pr_state`.

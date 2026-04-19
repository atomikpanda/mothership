# `mship finish` — idempotent, shared-git_root-aware, sets upstream — Design

## Context

Real-session feedback from 2026-04-18 flagged three tightly coupled sharp edges on `mship finish`:

1. **Not idempotent when a PR already exists.** Under a task whose affected repos (`infra`, `tailrd`, `web`) all shared `git_root: tailrd` and the same branch, the first run pushed + created PR #17 under `infra`, then errored on `tailrd` with gh's `a pull request for branch ... already exists`. `finished_at` never stamped; retrying produced the same error forever. The workaround is `mship finish --force`, whose semantics (re-push post-finish commits) don't match "harvest the existing PR and proceed."

2. **Shared `git_root` isn't reflected in PR tracking.** `state.tasks[slug].pr_urls` ended with `{"infra": "#17"}` only, even though `tailrd` and `web` are the same gh PR (same branch on the same git remote). Downstream users (`mship reconcile`, `mship status`) couldn't tell the other two repos had been pushed.

3. **`mship audit` reports `no_upstream` on all three repos after finish.** `git push -u origin <branch>` should have set tracking config; audit still flags `no_upstream`. The existing `without_no_upstream_on_task_branch` filter only fires when `task.finished_at is None`, so after finish stamps the task, the audit starts blocking on an upstream that actually exists.

Root cause for #1 and #2 is the same: `mship finish` treats each `affected_repos` entry as an independent gh PR, even when multiple entries resolve to the same `(effective_path, branch)` and therefore the same GitHub PR. Root cause for #3 is separate — the push may have set tracking config, but the gate doesn't verify it, and the audit filter's `finished_at` condition makes the gate correct only before finish runs.

All three are observable in one session; a single bundled fix is appropriate.

## Goal

1. **Finish groups affected repos by `(git_root_or_self, branch)`** so one push + one `gh pr create` covers every repo in the group, and the resulting URL is recorded on all group members.
2. **Finish is idempotent.** A retry after a mid-loop crash (or after a PR was already created — by mship or by the user via gh) succeeds by harvesting the existing URL instead of re-creating.
3. **Post-push verifies upstream tracking.** After `git push -u origin <branch>`, finish confirms `@{u}` resolves for the branch. If not (belt-and-suspenders — `-u` normally sets it), finish explicitly calls `git branch --set-upstream-to=origin/<branch> <branch>`. The existing `no_upstream` audit filter (with its `finished_at is None` condition) stays untouched — close-safety preserved.

## Success criterion

Bailey's scenario (a 3-repo shared-`git_root` workspace with the same feature branch) runs cleanly:

```
$ mship finish
api     | <output>
...
Opened 1 PR covering 3 repos:
  infra, tailrd, web → https://github.com/atomikpanda/tailrd/pull/17
$ mship finish        # retry
Already opened:
  infra, tailrd, web → https://github.com/atomikpanda/tailrd/pull/17
(nothing to push; finished_at stamped)
```

`state.tasks[slug].pr_urls` after either run: `{"infra": "...#17", "tailrd": "...#17", "web": "...#17"}`.

`mship audit` run immediately after `mship finish` does NOT report `no_upstream` on any of the three repos.

## Anti-goals

- **No change to the audit filter's `finished_at is None` condition.** Close-safety depends on the gate surfacing `no_upstream` when a task's branch truly lacks an origin tracking ref.
- **No auto-close on retry.** Idempotency means "safely re-run without error or double-opening PRs," not "auto-advance to `mship close`."
- **No extension to non-shared-path repos.** If two `affected_repos` have different effective paths, they remain separate PR groups (they're separate GitHub repos — one PR each).
- **No new CLI flags.** `--force` keeps its current semantics (push post-finish commits, reuse existing PR URL from state). The grouping + harvest path is the default behavior.
- **No retroactive auto-patch for existing stuck state.** A task that's currently stuck in `phase: review` with `finished_at=None` (Bailey's live `fix-taskfile-dev-leakage-...`) won't be auto-fixed by installing this. User still needs to merge #17 and `mship close`; subsequent tasks benefit.
- **No change to `gh pr create` arguments.** Same `--title`, `--body`, `--head`, `--base` as today.
- **No change to how coordination blocks are built**, beyond naming the grouped repos once instead of printing duplicate rows.

## Architecture

### New helpers in `src/mship/core/pr.py`

**`PRManager.ensure_upstream(repo_path: Path, branch: str) -> None`**

Idempotent verify-and-fix:

```python
def ensure_upstream(self, repo_path: Path, branch: str) -> None:
    """Ensure `branch`'s tracking ref resolves. No-op when already set."""
    check = self._shell.run(
        "git rev-parse --abbrev-ref --symbolic-full-name @{u}",
        cwd=repo_path,
    )
    if check.returncode == 0:
        return
    # `git push -u` normally sets tracking; this is belt-and-suspenders.
    self._shell.run(
        f"git branch --set-upstream-to=origin/{shlex.quote(branch)} {shlex.quote(branch)}",
        cwd=repo_path,
    )
```

Called immediately after `push_branch`. Silent on success; logs a warning only if the set-upstream itself fails (which implies `origin/<branch>` doesn't exist, which shouldn't happen post-push).

**`PRManager.list_pr_for_branch(repo_path: Path, branch: str) -> str | None`**

Returns the URL of an existing PR (any state) for `branch`, or `None`:

```python
def list_pr_for_branch(self, repo_path: Path, branch: str) -> str | None:
    result = self._shell.run(
        f"gh pr list --head {shlex.quote(branch)} --state all "
        f"--json url -q '.[0].url'",
        cwd=repo_path,
    )
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None
```

Used two places: pre-check before `create_pr` (grouping's happy path) and fallback parse on duplicate-PR stderr.

**`PRManager.create_pr` — duplicate-PR fallback**

Current behavior: `gh pr create` errors are raised as `RuntimeError`. New: if stderr matches the duplicate pattern, harvest via `list_pr_for_branch` and return the URL instead of raising.

```python
def create_pr(self, repo_path, branch, title, body, base=None) -> str:
    ...
    result = self._shell.run(cmd, cwd=repo_path)
    if result.returncode != 0:
        stderr = result.stderr.lower()
        if "already exists" in stderr and "pull request" in stderr:
            existing = self.list_pr_for_branch(repo_path, branch)
            if existing is not None:
                return existing
        raise RuntimeError(f"Failed to create PR: {result.stderr.strip()}")
    return result.stdout.strip()
```

The `lower()`-based match keeps the heuristic loose enough to survive gh minor-version changes in the exact phrasing.

### Grouping in `src/mship/cli/worktree.py::finish`

New helper (can live in `src/mship/cli/_finish_groups.py` or inline in `worktree.py` — inline is fine given its small size):

```python
@dataclass
class PRGroup:
    key: tuple[str, str]                    # (git_root_or_self, branch)
    members: list[str]                      # repo names in group, topo-ordered
    rep_name: str                           # representative repo (prefer parent)
    rep_path: Path                          # effective path to run push/create from
    base: str | None                        # effective base for this group


def _build_pr_groups(
    affected_repos: list[str],
    config: WorkspaceConfig,
    task: Task,
    effective_bases: dict[str, str | None],
) -> list[PRGroup]:
    """Group repos that share (git_root_or_self, branch, base) into one PR.

    For repos with `git_root`, the group key uses the git_root name. For
    repos without git_root (or whose git_root isn't in affected_repos),
    the key uses the repo's own name.

    Raises ValueError if a group has heterogeneous bases (defensive — not
    expected for shared git_root; split into subgroups if it ever happens).
    """
```

Group key: `(git_root_or_self, branch, base)` — branch is always `task.branch` in finish, so it's a tie-breaker for clarity rather than a distinguishing field. Base is a distinguishing field for the defensive split.

Representative selection:
1. If the group's `git_root_or_self` name is in `affected_repos`, pick it (it's the parent).
2. Else, pick the first member in topo order.

This ensures `rep_path` is the shared repo root (tailrd) rather than a subdirectory (web) — makes `git push` and `gh` calls happen from the top-level path even when the parent isn't in `affected_repos`.

### `finish` repo loop rewritten to groups

The existing repo loop (~lines 700–820 of `worktree.py`) iterates `affected_repos` directly. The rewrite iterates `_build_pr_groups(...)` and, for each group:

1. **Push from `rep_path`:** `pr_mgr.push_branch(rep_path, task.branch)`.
2. **Ensure upstream:** `pr_mgr.ensure_upstream(rep_path, task.branch)`.
3. **Check for existing PR:** `existing = pr_mgr.list_pr_for_branch(rep_path, task.branch)`. If `existing is not None`, skip to step 5.
4. **Create PR:** `url = pr_mgr.create_pr(rep_path, task.branch, title, body, base=group.base)`. The duplicate-PR fallback in `create_pr` handles the race where someone else opened it between our list and create.
5. **Record URL on every group member:**
   ```python
   def _record(s, members=group.members, u=url):
       for name in members:
           s.tasks[task_slug].pr_urls[name] = u
   state_mgr.mutate(_record)
   ```
6. Append one `pr_list` entry per group (with `members` listed) so the coordination block renders once per group, not once per repo.

Existing `--force` re-push path (when `repo_name in task.pr_urls and force`) still works — treated as single-member group with known URL. The existing branch-level re-push logic is compatible.

### Coordination block unchanged, with one display tweak

`build_coordination_block` renders `pr_list`. After this change, `pr_list` has one entry per group, e.g.:

```python
{"repo": "tailrd", "members": ["infra", "tailrd", "web"], "url": "...#17", "order": 1, "base": "main"}
```

Update the template to render `"tailrd (+infra, web)"` when `members` has >1 entry. Single-member groups render identically to today.

### Nothing else changes

- `push_branch` — unchanged.
- The `--force` re-push branch — unchanged in behavior; just operates on groups of size 1.
- `pr_urls` dict shape — unchanged (`dict[repo_name, url]`), just fully populated for shared-git_root members.
- State schema — unchanged. No migration.
- Audit filter — unchanged. Close-safety preserved.
- Reconcile — unchanged. Now naturally sees the same URL under multiple repo keys, which matches reality.

## Data flow

**Fresh `mship finish` on a 3-repo shared-git_root task:**

1. CLI resolves task, computes `effective_bases` per repo.
2. `_build_pr_groups` returns one group: `key=("tailrd", "feat/X"), members=["infra", "tailrd", "web"], rep_name="tailrd", rep_path=<tailrd worktree>, base="main"`.
3. Push once from `tailrd`'s worktree → `-u` sets tracking config in the shared .git/config.
4. `ensure_upstream` verifies `@{u}` resolves → no-op.
5. `list_pr_for_branch` → None (fresh).
6. `create_pr` → PR #17 URL.
7. Record URL on `infra`, `tailrd`, `web` in one state mutation.
8. `pr_list = [{"repo": "tailrd", "members": ["infra","tailrd","web"], "url": "...#17", ...}]`.
9. Coordination block (single-group case): no block added (only rendered when `len(pr_list) > 1`). If there are multiple groups total, block lists the `tailrd (+infra, web)` entry once.
10. Stamp `finished_at`.

**Retry after mid-loop crash** (e.g., hypothetically the loop crashed between step 6 and step 7):

1. Same group construction.
2. Push → `branch already on origin`, no-op.
3. `ensure_upstream` → no-op.
4. `list_pr_for_branch` → returns the existing PR URL.
5. Skip `create_pr`.
6. Record URL on all 3. Stamp.

**External-PR case** (user opened a PR via `gh pr create` outside mship):

1. `list_pr_for_branch` → returns the externally-created URL.
2. Skip `create_pr`. Record URL on all group members. Stamp.

**Race where someone creates a PR between our list-check and create_pr:**

1. `list_pr_for_branch` → None at time T0.
2. `create_pr` → gh errors with "already exists" (T1).
3. `create_pr` fallback re-lists → harvests URL. Returns it.
4. Same downstream.

**Missing upstream after push** (genuinely broken `git push -u`, or user deleted `branch.<name>.remote` config between push and ensure):

1. Push succeeds.
2. `ensure_upstream` sees `@{u}` fails.
3. Explicit `git branch --set-upstream-to=origin/<branch> <branch>` → succeeds (origin ref exists post-push).
4. Subsequent audit reads `@{u}` → resolves. No `no_upstream`.

## Error handling

- **`gh pr list` fails** (network, auth): `list_pr_for_branch` returns None. `create_pr` proceeds. If gh is truly down, `create_pr` also fails and user sees the usual error. No new failure modes.
- **`ensure_upstream`'s `git branch --set-upstream-to` fails**: unexpected; origin ref should exist because push just succeeded. Log a warning via the existing output channel; don't abort finish. The audit will surface `no_upstream` on subsequent runs, which is the current behavior — not a regression.
- **Heterogeneous bases within a group**: `_build_pr_groups` raises `ValueError("group <key> has mixed effective_bases: …")`. Not expected for shared git_root (all members resolve identical bases). If it ever fires, user gets a clear error rather than a silent wrong-base PR.
- **Duplicate-PR stderr doesn't parse**: `create_pr` falls through to the original `RuntimeError` path. User sees today's error message — no regression.
- **Crash between steps 6 and 7** (record mutation before stamp): `pr_urls` populated for all group members; `finished_at` not set. Next retry's `list_pr_for_branch` harvests the same URL, re-records (idempotent), stamps. Clean recovery.

## Testing

### Unit — `tests/core/test_pr.py` (extend existing)

1. `ensure_upstream` no-op when `@{u}` resolves. Mock shell returns rc=0 for the `rev-parse` call; assert no second shell call.
2. `ensure_upstream` runs `git branch --set-upstream-to=origin/<branch> <branch>` when `rev-parse` fails. Mock first call rc=1; assert second call issued with exact command string.
3. `list_pr_for_branch` returns None when `gh pr list` returns empty stdout.
4. `list_pr_for_branch` returns the URL when stdout is a single-line URL.
5. `list_pr_for_branch` returns None on gh non-zero exit.
6. `create_pr` happy path unchanged: mock gh returns URL → method returns it.
7. `create_pr` duplicate-PR fallback: mock gh rc=1 with stderr containing `"a pull request for branch X already exists"`, mock `list_pr_for_branch` to return a URL → method returns the harvested URL.
8. `create_pr` duplicate-PR fallback when `list_pr_for_branch` also fails: falls through to RuntimeError (no new behavior).
9. `create_pr` non-duplicate rc=1 → RuntimeError (regression — existing behavior).

### Unit — `tests/cli/test_finish_groups.py` (new)

1. Three repos all sharing `git_root=tailrd` in affected_repos → one group of 3.
2. Two repos sharing `git_root=tailrd`, one standalone `api` → two groups.
3. All standalone → n groups of size 1.
4. Group representative selection: group's git_root IS in affected_repos → rep_name = git_root.
5. Group representative selection: git_root is NOT in affected_repos (pathological but possible if user passes `--repos`) → rep_name = first member in topo order.
6. Heterogeneous bases within a group (construct a scenario): raises `ValueError`.
7. `rep_path` for shared-git_root group is the git_root's effective path, not a subdirectory.

### Integration — `tests/cli/test_finish.py` (extend)

Use a `file://` bare repo as origin so `git push -u` exercises the real tracking-config path, while mocking the gh calls.

1. Two-repo shared-git_root first run: fixture creates config with `infra` + `tailrd` sharing path; mocked shell records `gh pr create` being called ONCE (not twice); state.pr_urls contains both with the same URL.
2. Retry after simulated mid-loop crash: pre-populate state with `pr_urls={"infra": None}` (or similar), run finish; mocked `gh pr list` returns the existing URL; `gh pr create` not called; state.pr_urls has both repos.
3. External-PR (user opened PR manually): mock `gh pr list` to return URL immediately; assert `create_pr` not invoked; URL recorded.
4. `create_pr` duplicate-stderr race: mock `gh pr list` returns None on first call, then `gh pr create` rc=1 with duplicate stderr, then `gh pr list` (inside fallback) returns URL → assert harvested URL is recorded.
5. Post-push upstream verification: after finish, `git rev-parse --abbrev-ref --symbolic-full-name @{u}` in the worktree returns `origin/<branch>`.
6. `mship audit` run immediately after finish (no task anchor) reports NO `no_upstream` issue on the repo. Uses the file:// origin; real push + real config.

### Regression

- Existing `test_finish.py` tests for single-repo tasks stay green.
- Existing `test_pr.py` tests for `push_branch` and `create_pr` happy path stay green.
- Full `pytest tests/` green.

### No manual smoke

Per scope decision: the test matrix above covers the grouping, harvest, and upstream-verification surface. A real-gh smoke would add a live PR create on Bailey's GitHub account, which we've scoped out. If a future issue shows a gap between mocked gh and real gh, we extend integration tests.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | Group by `(git_root_or_self, branch, base)`, one PR per group | Structurally represents what's actually true: repos sharing git_root on the same branch ARE the same GitHub PR. Fixes #1 and #2 at the source instead of retro-patching error handling. |
| 2 | Duplicate-PR stderr fallback as belt-and-suspenders | Covers externally-created PRs and races between list + create. Low cost, high safety net. |
| 3 | Post-push `ensure_upstream` verification, NOT filter-loosen | Preserves the `no_upstream` gate's close-safety role (protecting against deleting unpushed work). Keeps the filter's `finished_at is None` condition intact. |
| 4 | No new CLI flags | Grouping is the default behavior; `--force` keeps existing semantics. Users don't need to opt in. |
| 5 | No auto-patch for existing stuck state | Bailey's current stuck task (#17 + null `finished_at`) needs manual unblock. Installing this fix prevents the bug going forward but doesn't rewrite history. |
| 6 | `gh pr list --state all` (not just `--state open`) | An existing closed/merged PR for the same branch is still the target to harvest. Edge case but cheap to cover. |
| 7 | Representative selection prefers git_root parent | `git push` and `gh` run from the parent path; shared .git/config is affected equally but semantics read more naturally. |
| 8 | Coordination block shows grouped repos once with `(+members)` | Avoids three near-identical rows when three repos share a PR. Minimal template change. |

---
name: finishing-a-development-branch
description: Use when implementation is complete, all tests pass, and you need to decide how to integrate the work
---

# Finishing a Development Branch

## Overview

**Core principle:** Verify tests → Detect environment → Present options → Execute choice → Clean up.

**Announce at start:** "I'm using the finishing-a-development-branch skill to complete this work."

## Step 1: Verify Tests

Run the project's full test suite (`npm test` / `cargo test` / `pytest` / `go test ./...`).

**In a mothership workspace, run `mship test` instead of a bare runner** — it records the result as evidence, so `mship finish`'s evidence gate has something to read. Bare runners are for non-mship repos.

**If tests fail**, report the failures and stop — the menu comes after a green suite:

```
Tests failing (<N> failures). Must fix before completing:

[Show failures]
```

**If tests pass:** continue to Step 2.

## Step 2: Detect Environment

```bash
GIT_DIR=$(cd "$(git rev-parse --git-dir)" 2>/dev/null && pwd -P)
GIT_COMMON=$(cd "$(git rev-parse --git-common-dir)" 2>/dev/null && pwd -P)
# Capture now, while still inside the workspace — Step 5 changes directory
# before cleanup (Step 6) needs this value
WORKTREE_PATH=$(git rev-parse --show-toplevel)
```

This determines which menu to show and how cleanup works:

| State | Menu | Cleanup |
|-------|------|---------|
| `GIT_DIR == GIT_COMMON` (normal repo) | Standard 3 options | No worktree to clean up |
| `GIT_DIR != GIT_COMMON`, named branch | Standard 3 options | Provenance-based (see Step 6) |
| `GIT_DIR != GIT_COMMON`, detached HEAD | Reduced 2 options (no merge) | Externally managed — leave in place |

## Step 3: Determine Base Branch

The base branch is whatever this work forked from — usually named in the
plan, the conversation, or the branch's upstream. If it is not already
known, ask: "This branch split from <your best guess> - is that correct?"
Confirm before merging: merging into the wrong base is expensive to undo.

## Step 4: Present Options

**Normal repo and named-branch worktree — present exactly these 3 options:**

```
Implementation complete. What would you like to do?

1. Merge back to <base-branch> locally
2. Push and create a Pull Request
3. Keep the branch as-is (I'll handle it later)

Which option?
```

**Detached HEAD — present exactly these 2 options:**

```
Implementation complete. You're on a detached HEAD (externally managed workspace).

1. Push as new branch and create a Pull Request
2. Keep as-is (I'll handle it later)

Which option?
```

Present the menu exactly as written — concise, with every option coming
from the list above. Discarding the work happens only in response to your
human partner explicitly asking for it (see "If your human partner asks to
discard the work" below). Wait for their answer; the integration decision
is theirs.

## Step 5: Execute Choice

### Option 1: Merge Locally

*In a mothership workspace, Option 1 is not the normal path — Option 2 (`mship finish` → PR → merge auto-advance) is. When your human partner explicitly chooses local-merge-no-PR, use the sanctioned sequence, IN THIS ORDER: run `mship finish --push-only` FIRST (stamps the task finished, pushes the branches, opens no PR), then do the merge below, then plain `mship close` LAST — close verifies the merge actually reached the base and only then advances the lifecycle exactly like a merged-PR close (bound spec → `implemented`, WorkItem → `done`), recording the task as "no PRs (pushed via --push-only)". Closing before the merge still works (the pushed branch is recoverable) but advances nothing — close notes "branch pushed but not merged; lifecycle not advanced". Never route a successful merge through `mship close --abandon`: that records delivered work as cancelled. `--abandon` is strictly for genuinely discarding work.*

```bash
# Get main repo root for CWD safety
MAIN_ROOT=$(git -C "$(git rev-parse --git-common-dir)/.." rev-parse --show-toplevel)
cd "$MAIN_ROOT"

# Merge first — verify success before removing anything
git checkout <base-branch>
git pull
git merge <feature-branch>

# Verify tests on merged result
<test command>
```

If tests fail on the merged result: stop, leave the worktree and branch in
place, and investigate — nothing has been pushed, so the merge is local
and recoverable.

Once the merged result is green: clean up the worktree (Step 6), then
delete the branch:

```bash
git branch -d <feature-branch>
```

### Option 2: Push and Create PR

**In a mothership workspace**, don't push or open the PR by hand — write
the PR body to a file and let `mship finish` do all of it:

```bash
cat > /tmp/pr-body.md <<'EOF'
## Summary
<2-3 bullets of what changed>

## Test plan
- [ ] <verification steps>
EOF

# Pushes the branch, opens the PR, stamps state; blocks without passing test evidence
mship finish --require-tests --body-file /tmp/pr-body.md
```

**Post-finish iteration (reviewer feedback, CI fixes, typos):** don't
spawn a new task. Stage the fix in the worktree and run
`mship commit "<msg>"` — it commits across the task's affected repos,
pushes to the existing PR, and journals per repo. Use this for small
iterations until the PR merges.

**Outside a mothership workspace**, push and create the PR with the
forge's tooling:

```bash
git push -u origin <feature-branch>
# From a detached HEAD, name the new branch on the remote:
# git push origin HEAD:refs/heads/<new-branch>
```

Then create the pull/merge request against <base-branch> with the forge's
tooling — its CLI if one is available, or the creation URL most forges
print when you push — following the repo's PR template and conventions if
present, and report the URL to your human partner.

Keep the worktree — your human partner iterates on PR feedback there.

### Option 3: Keep As-Is

Report: "Keeping branch <name>. Worktree preserved at <path>."

### If your human partner asks to discard the work

This path exists only as a response to an explicit request to throw the
work away. Confirm first:

```
This will permanently delete:
- Branch <name>
- All commits: <commit-list>
- Worktree at <path>

Type 'discard' to confirm.
```

Wait for that exact confirmation. When it arrives, in a mothership
workspace one command abandons the task, deletes the branch, and cleans
the worktree in state:

```bash
mship close --abandon
```

Outside a mothership workspace:

```bash
MAIN_ROOT=$(git -C "$(git rev-parse --git-common-dir)/.." rev-parse --show-toplevel)
cd "$MAIN_ROOT"
```

Then clean up the worktree (Step 6) and force-delete the branch:

```bash
git branch -D <feature-branch>
```

## Step 6: Cleanup Workspace

**Runs for Option 1 and confirmed discards.** Options 2 and 3 always
preserve the worktree. Both callers have already changed directory to the
main repo root — worktree removal must run from outside the worktree —
and use the `GIT_DIR`/`GIT_COMMON`/`WORKTREE_PATH` values captured in
Step 2, from before that directory change.

**In a mothership workspace, `mship close` owns cleanup — no manual
`git worktree remove`.** After a local merge (Option 1, preceded by
`mship finish --push-only` as described above), run plain `mship close`;
after a confirmed discard, `mship close --abandon`.
After a PR merges (Option 2), you usually run nothing at all: when
`mship serve` is running, its watcher **auto-advances** the bound spec
(`dispatched → implemented`) and its WorkItem (→ `done`, clearing
`needs_review`) and tears the worktree down — zero manual steps in the
clean case (check via `mship pr` for an aggregate view across tasks, or
`mship reconcile` for drift). You only need `mship close` when the
worktree still has **uncommitted or unpushed** changes: auto-teardown
skips it to protect that work (the spec/WorkItem still advance, and a
note is posted on the task's thread) — resolve (commit/push) then
`mship close`, or `mship close --force` to discard. `--force` is the
only way to delete a dirty/unpushed worktree, and the guard is universal
(it also applies to `mship close --abandon`), so uncommitted/unpushed
work is **never** deleted without `--force`.

**If `GIT_DIR == GIT_COMMON`:** Normal repo, no worktree to clean up. Done.

**If `WORKTREE_PATH` is under `.worktrees/` or `worktrees/`:** Superpowers
created this worktree — we own cleanup:

```bash
git worktree remove "$WORKTREE_PATH"
git worktree prune  # Self-healing: clean up any stale registrations
```

**Otherwise:** The host environment owns this workspace — leave it in
place. If your platform provides a workspace-exit tool, use it.

## Quick Reference

| Option | Merge | Push | Keep Worktree | Cleanup Branch |
|--------|-------|------|---------------|----------------|
| 1. Merge locally | yes | - | - | yes |
| 2. Create PR | - | yes | yes | - |
| 3. Keep as-is | - | - | yes | - |
| Discard (explicit request only) | - | - | - | yes (force) |

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "Tests passed earlier this session" | Run the suite on the tree you are about to integrate. A green run only proves the tree it ran on. |
| "They obviously want it merged" | Integration is your human partner's decision. Present the menu and wait. |
| "They seem done with this feature — I'll offer to discard it" | The menu is complete as written. Discard happens only when your human partner asks for it in so many words. |
| "'Yeah, get rid of it' counts as confirmation" | Only the typed word `discard` authorizes deletion. |
| "The PR is up, so the worktree is clutter now" | PR feedback gets fixed in that worktree. It stays until the work lands. |
| "This other worktree looks stale — I'll clean it too" | Clean up only worktrees under `.worktrees/` or `worktrees/`. Everything else belongs to the host. |
| "The merged-result failure is probably flaky" | A failing merged result stops everything. Branch and worktree stay put while you investigate. |
| "The base branch is obviously main" | Confirm the fork point or ask. Merging into the wrong base is expensive to undo. |
| "The push was rejected — force-push will fix it" | A rejected push means the remote moved. Investigate; force-push only on your human partner's explicit request. |

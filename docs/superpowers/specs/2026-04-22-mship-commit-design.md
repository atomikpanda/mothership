# `mship commit` — Design

Closes #29.

## Problem

`mship finish` treats the PR opening as essentially terminal. Real life keeps happening after: reviewer comments, CI fixes, last-mile doc updates. Today's guidance says "open a new task," which is ceremony overhead that swamps the benefit — most PR iterations are 1–3 small commits on the same branch.

Infrastructure already exists for post-finish work: `mship journal` and `mship test` have no finish-guard; `mship finish --force` re-pushes existing commits. What's missing is a sanctioned one-shot that COMMITS the new changes AND coordinates the mship-side narrative (push + journal) so the task's story stays coherent.

## Solution

New command `mship commit "<msg>"` that:

1. Stages-required: operates only on `git diff --cached` changes. User controls selection via `git add`.
2. Iterates `task.affected_repos` — any worktree with staged changes gets a commit.
3. Post-finish: also pushes to the PR. Pre-finish: local commit only.
4. Journals one entry per repo committed, preserving narrative accuracy.

## Why `commit` (not `patch`)

Literal git vocabulary. The command IS a commit; jargony names like `patch` restrict it to "small fix for existing PR" and create cognitive overhead. `commit` stays honest and extensible — if future uses emerge (pre-finish quick-commit without cd'ing), the name already fits.

## Scope

### In scope
- New command `mship commit "<msg>"` in `src/mship/cli/commit.py`.
- Iterates `task.affected_repos` in topo order.
- Per-repo: check staged → commit → optionally push → journal.
- Non-fatal per-repo skip when no staged changes (one repo with nothing staged isn't an error as long as some other repo had staged changes).
- Hard error when NO repo has staged changes across all affected_repos.
- Output: TTY summary (per repo: committed / pushed / skipped); JSON for non-TTY.

### Out of scope
- `--amend` — fundamentally different operation; use `git commit --amend` directly.
- `--no-verify` / hook bypass — hooks exist for a reason; respect them.
- `-a` / auto-stage — user staging via `git add` is the selection mechanism.
- Pre-finish push — before `mship finish`, no PR exists to push to.
- Per-repo different messages in one invocation — do them one at a time.
- Post-commit coordination block update — `mship finish` already handles multi-repo coordination; `commit` just appends commits to existing PRs.
- Changes to `mship finish --force` — orthogonal (re-pushes existing commits; `commit` creates the new commits to push).

## Architecture

Single new file. Reuses existing building blocks:

```
mship.cli.commit.commit(message: str, task: str | None)
  ├─ resolve_for_command("commit", state, task, output)
  │    └─ existing helper: task lookup + TTY breadcrumb + JSON resolution fields
  ├─ for each repo in task.affected_repos:
  │    ├─ worktree = task.worktrees[repo]
  │    ├─ staged? → `git diff --cached --quiet` (exit 1 means staged present)
  │    ├─ if not staged: add to skipped list, continue
  │    ├─ commit: `git commit -m <msg>` via container.shell()
  │    │    └─ non-zero exit → fail the whole command (no partial state)
  │    ├─ capture commit SHA: `git rev-parse HEAD`
  │    ├─ if task.finished_at and task.pr_urls.get(repo):
  │    │    └─ `git push` in worktree → fail whole command on non-zero
  │    └─ log_manager.append(slug, message=msg, repo=repo, action="committed")
  └─ if every repo was skipped → error "nothing staged in any worktree"
```

## Flow

```
$ git -C path/to/shared/.worktrees/feat/xyz add src/foo.py
$ git -C path/to/api-gateway/.worktrees/feat/xyz add docs/bar.md
$ mship commit "fix: address reviewer feedback"

→ task: xyz  (resolved via cwd)
  shared: committed 7f3a1b2 → pushed to https://github.com/o/r/pull/42
  api-gateway: committed e9c2d8f → pushed to https://github.com/o/r/pull/43
```

Pre-finish equivalent:
```
$ mship commit "fix: typo"

→ task: xyz  (resolved via cwd)
  shared: committed 7f3a1b2 (not pushed — task not finished)
```

Nothing staged:
```
$ mship commit "x"

→ task: xyz  (resolved via cwd)
ERROR: nothing staged in any affected repo. Run `git add <files>` first.
```

## Journal entry shape

One entry per repo committed:

```
## 2026-04-22T19:33:12Z  repo=shared  action=committed
fix: address reviewer feedback
```

Pre-existing `test_state` / `iteration` / etc. are unaffected — this is just a new action value.

## Output format

**TTY** (one line per repo):
```
  <repo>: committed <short-sha>[ → pushed to <pr_url>][ (not pushed — task not finished)][ (skipped — nothing staged)]
```

**Non-TTY JSON:**
```json
{
  "task": "xyz",
  "repos": [
    {"repo": "shared", "commit_sha": "7f3a1b2...", "pushed": true, "pr_url": "https://..."},
    {"repo": "api-gateway", "commit_sha": "e9c2d8f...", "pushed": true, "pr_url": "https://..."},
    {"repo": "infra", "skipped": "nothing staged"}
  ],
  "resolved_task": "xyz",
  "resolution_source": "cwd"
}
```

## Error modes

| Condition | Behavior |
|---|---|
| No task resolved | Existing `resolve_for_command` error path |
| Staged in zero repos | Exit 1 with "nothing staged in any affected repo" |
| `git commit` fails (hook rejection, etc.) in any repo | Exit 1. Commits in earlier repos are NOT rolled back (git has no transaction). Error message names the failing repo and includes git's stderr. |
| `git push` fails post-finish | Exit 1. Commit is already made locally; user can retry push via `git push` or `mship finish --force`. Error names the repo. |
| No `finished_at` but the user wanted to push | Not an error — pre-finish local commit is valid; user runs `mship finish` later. |

## Testing

### Integration — `tests/cli/test_commit.py` (new)

- **Pre-finish commit, single repo**: stage in worktree, run `mship commit msg`. Assert commit created, no push, one journal entry with `action="committed"`.
- **Pre-finish commit, multi-repo coordinated**: stage in two worktrees, run once. Assert two commits (same message), no pushes, two journal entries.
- **Post-finish commit, single repo**: set `finished_at` + `pr_urls`, stage, commit. Assert commit + push + journal.
- **Post-finish commit, multi-repo**: stage across two repos with PRs, commit. Assert 2 commits + 2 pushes + 2 journals.
- **Skip when partial**: stage in only one of two worktrees. Assert commit for the one, "skipped" note for the other, exit 0.
- **Hard error when nothing staged anywhere**: no staged changes, commit → exit 1 with clear message.
- **Commit failure surfaces**: mock `git commit` non-zero in one repo → exit 1, stderr surfaced.
- **Push failure post-finish surfaces**: mock `git push` non-zero → exit 1, commit sha still in journal (commit happened).

### Documentation

- Update `src/mship/skills/working-with-mothership/SKILL.md` to name `mship commit` as the sanctioned post-finish iteration tool (replacing or complementing the "open a new task" guidance for small fixes).

## Anti-goals

- No `--amend`.
- No `--no-verify` / hook bypass.
- No auto-stage.
- No pre-finish push.
- No new flags for multi-repo (staging is the selection mechanism).
- No rollback on partial-commit failure (git has no transaction; best we can do is clear error messages).

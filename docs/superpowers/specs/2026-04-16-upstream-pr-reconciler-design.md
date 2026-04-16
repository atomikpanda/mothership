# Design — `mship reconcile`: upstream PR drift detection

**Status:** Approved
**Date:** 2026-04-16
**Author:** Bailey Seymour (with Claude)

## Problem

Local mship task state can silently diverge from upstream reality:

- A teammate merges your PR; you keep editing the worktree and `git push` happily updates the dead branch.
- A reviewer rebases your PR before merging; your local HEAD no longer matches.
- A stacked PR's base branch gets merged into main; your PR is now on stale ground.
- You close a PR but forget to run `mship close`; `mship status` shows a healthy task.

We hit (1) during dogfooding: a commit went to an already-merged branch and vanished. The fix shipped, but the class of bug is broader than just "PR merged." `mship audit` covers local-git drift; there's no peer for upstream drift.

This spec adds **`mship reconcile`** — a per-user control-plane loop that reconciles local task state against GitHub's view, treats the result as named state transitions, and blocks destructive commands on unresolved drift.

## Goals

- Detect four classes of drift per task: `merged`, `closed`, `diverged`, `base_changed`.
- Block `spawn`, `finish`, `close`, and pre-commit on unresolved drift with specific, actionable messages.
- Provide a user-facing `mship reconcile` command for explicit checks and debugging.
- Cache `gh` responses with a 5-minute TTL so the cost is bounded.
- Degrade cleanly offline: fall back to last cache or proceed without blocking.
- Support collaborative settings: query by branch, not author — detect teammate-initiated transitions.

## Non-Goals

- **Local git-state drift** — `mship audit` already owns this. No merging.
- **Auto-heal** — every block is a warning with a recovery hint; no destructive auto-action (e.g. auto-close). Revisit in a later iteration.
- **Background daemon** — reconciliation runs at entry points and on-demand, not continuously.
- **Cross-workspace reconciliation** — scope is a single mship workspace.

## Architecture

### Section 1 — Detection & state model

One batched `gh` call per reconciliation covers every task:

```bash
gh pr list --search "head:<branch-1> head:<branch-2> …" --state all \
  --json headRefName,state,isDraft,baseRefName,mergeCommit,url,updatedAt
```

Per task, the reconciler computes an `UpstreamState`:

- `in_sync` — PR open, local HEAD reachable from remote branch, base unchanged.
- `merged` — PR state == `MERGED`.
- `closed` — PR state == `CLOSED` (no merge).
- `diverged` — PR open, but `git rev-list --left-right @{u}...HEAD` shows remote-only commits.
- `base_changed` — PR open, but `baseRefName` != the base stored at spawn.
- `missing` — branch exists locally, no PR at that head (informational, never blocks).

Transitions are computed, not stored. The only state-model change is adding `base_branch: str | None` to `Task`, set at spawn to the workspace's default branch (back-fills to `main` on first reconcile for pre-existing tasks).

**File:** `src/mship/core/reconcile/detect.py` — pure function `detect(tasks, gh_response, git_inspector) -> dict[slug, UpstreamState]`.

### Section 2 — Blocking matrix

| State | `spawn` | `finish` | pre-commit | `close` | read-only |
|---|---|---|---|---|---|
| `in_sync` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `merged` | ❌ | ❌ | ❌ | ✅ (the exit) | ✅ |
| `closed` | ❌ | ❌ | ❌ | ✅ (prompts `--abandon`) | ✅ |
| `diverged` | ⚠️ warn | ❌ | ❌ | ✅ | ✅ |
| `base_changed` | ⚠️ warn | ❌ | ✅ | ✅ | ✅ |
| `missing` | ✅ | ✅ | ✅ | ✅ | ✅ |

Block messages name the command, task, transition, and recovery:

> `mship finish` refused: task `feat/foo` has `base_changed` drift (PR #43 base is now `develop`, you branched from `main`). Run `mship reconcile` for details, then rebase or pass `--bypass-reconcile`.

**Escape hatches** are scoped and explicit:

- `mship <cmd> --bypass-reconcile` — override once for this invocation.
- `mship reconcile --ignore=<slug>` — persistently ignore drift on one task; stored in the cache. Cleared on `mship close`.

No blanket "skip all checks" toggle.

### Section 3 — Caching, output, `mship reconcile` UX

**Cache:** `.mothership/reconcile.cache.json`

```json
{
  "fetched_at": "2026-04-15T23:45:00Z",
  "ttl_seconds": 300,
  "results": {
    "feat/foo": {"state": "merged", "pr_url": "...", "merged_at": "...", "base": "main"}
  },
  "ignored": ["old-stale-slug"]
}
```

Entry points (`spawn`, `finish`, `close`, pre-commit) use cached results if fresh; otherwise run one batched `gh pr list` across every task's branch. `--bypass-reconcile` skips the fetch.

**Offline** (gh fails or returns non-zero): fall back to last cache if present; otherwise warn (`reconcile unavailable (offline)`) and proceed without blocking. Never fail closed on network errors.

**`mship reconcile` command:**

```
mship reconcile                     # re-run, print a table
mship reconcile --json              # structured output (agent path)
mship reconcile --ignore=<slug>     # persistently ignore drift
mship reconcile --clear-ignores     # reset ignores
mship reconcile --refresh           # skip cache, force refetch
```

TTY output:

```
Task                   State           PR    Action
feat/foo               ⚠ merged        #42   run `mship close`
feat/bar               ⚠ base_changed  #43   rebase onto main
feat/baz               ✓ in_sync       #44   —
```

Non-TTY: JSON (matches mship's existing convention — no flag needed).

### Section 4 — Testing, edge cases, migration

**Testing**

- Unit (`tests/core/reconcile/test_detect.py`) — fake gh response + fake git inspector, assert every `UpstreamState` case (`merged`, `closed`, `diverged`, `base_changed`, `missing`, `in_sync`).
- Unit — cache read/write, TTL expiry, ignore-list add/clear.
- Integration (`tests/cli/test_reconcile.py`) — Typer runner against `mship reconcile` with a mocked fetcher: table output, JSON output, exit 0 even when drift is detected.
- Integration — blocker behavior: `mship finish` with `merged` state exits non-zero; `--bypass-reconcile` lets it through.
- Offline path — gh fetcher raises → fall back to cache; no cache → proceed with `reconcile unavailable` warning, exit 0.

**Edge cases**

- **Rate-limit / auth failure** — treated as offline: warn, fall back, never block.
- **Task with no branch pushed** — `missing`; benign, informational.
- **Task with branch but no PR yet** (pre-`finish`) — equivalent to `missing`.
- **Multiple PRs at the same head** (rare, after re-open) — pick most recent by `updatedAt`, warn about duplicates.
- **Workspace with zero tasks** — reconcile is a no-op, exit 0.
- **Fresh clone** — `.mothership/reconcile.cache.json` absent; one live fetch on first entry point, then caches.

**Migration**

- `Task.base_branch: str | None = None` added to the pydantic model. Back-filled to the workspace's default branch on first reconcile when missing. No forced migration step.
- `.mothership/reconcile.cache.json` created lazily; safe to delete at any time.
- No changes to existing commands' positional args or primary flags. Adds `--bypass-reconcile` on `spawn`, `finish`, `close`, and the pre-commit check; adds new `mship reconcile` subcommand.
- Renaming `--force-audit` to `--bypass-audit` is **out of scope** for this spec — flagged as a follow-up when we next touch `audit`.

## Risk / Open Questions

- **Per-repo base-branch detection** — workspaces with repos whose default branch isn't `main` need real default-branch lookup (`gh repo view --json defaultBranchRef`). Cache once at workspace init.
- **Gh API shape drift** — pin to the JSON fields listed; if `--search head:` syntax changes, fall back to per-task `gh pr list --head <branch>` calls (N calls instead of 1). Implementation should isolate the fetcher behind an interface.
- **False-positive rate on `diverged`** — force-push after a squash-merge cleanup may look like divergence even when intentional. Surface it as `⚠️ warn` on most paths and full block only on `finish`/pre-commit, matching the matrix above.

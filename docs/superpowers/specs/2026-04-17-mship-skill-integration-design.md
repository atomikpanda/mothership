# mship-skill workflow integration — Design

## Context

Mship ships a bundle of skills (installed via `mship skill install` into `~/.claude/skills/` and `~/.agents/skills/mothership/`). Most are generic methodology skills inherited from upstream `obra/superpowers`; they apply equally to any workflow. A few prescribe concrete git/gh commands that directly conflict with mship's state-managing verbs — agents following those skills bypass `mship finish`'s audit gate, leave `finished_at` unstamped, and skip the state reconciliation that mship exists to provide.

Audit of the 15 bundled skills in this session found exactly four with mship-workflow touch points:

| Skill | Current conflict | Severity |
|---|---|---|
| `finishing-a-development-branch` | Option 2 teaches `git push -u origin <branch>` + `gh pr create --title "…" --body "…"`; Option 4 teaches `git branch -D`; Step 5 teaches `git worktree remove` | High — routes agents around `mship finish`/`mship close` |
| `using-git-worktrees` | Line 97 teaches `git worktree add "$path" -b "$BRANCH"` directly | High — routes around `mship spawn` |
| `executing-plans` | Partially mship-aware already (line 64-69 "Remember" bullet); finish-skill reference at line 34-37 doesn't note the mship flow | Low — polish |
| `writing-plans` | Illustrative `git commit -m "feat: …"` at line 102 is a plan template, not a workflow prescription — but benefits from showing the `mship journal` pairing authors should encode into mship plans | Very low — enhancement |

Ten other skills (`test-driven-development`, `systematic-debugging`, `verification-before-completion`, `writing-skills`, `brainstorming`, `requesting-code-review`, `receiving-code-review`, `dispatching-parallel-agents`, `using-superpowers`, `subagent-driven-development`, `working-with-mothership`) have no mship conflicts and stay untouched.

## Goal

An agent following any bundled skill in a mship workspace produces a flow that goes through mship's verbs (`spawn`, `finish --body-file`, `close`, `journal`) — the commands that update `.mothership/state.yaml`, trigger audit/reconcile gates, and preserve worktree isolation. No direct `git push -u origin` / `gh pr create` / `git worktree add` / `git branch -D` / `git worktree remove` in the canonical path.

Wording and structure of each skill are preserved per the "keep most of the skills as-is" directive; edits are surgical at the conflict sites, with one top-of-skill callout for `using-git-worktrees` where pure inline substitution would trivialize entire sections (decision 1 below).

## Anti-goals

- **No structural rewrites.** Headings, step numbers, option lists, table layouts preserved. Skills remain recognizable as the superpowers originals.
- **No conditional forks** (`if mship, else generic` branches scattered in prose). The mship-installed copies assume mship — they're distributed *by* mship.
- **No change to generic-methodology skills.** Ten of the fifteen stay byte-identical.
- **No upstream contribution to `obra/superpowers`.** These edits apply to the mship-bundled copies only; the upstream repo keeps its own versions.
- **No new skill.** `working-with-mothership` already fills the mship-overview role; we don't need an `mship-pr-flow` or similar.
- **No frontmatter / description changes.** Skill discovery triggers stay intact.

## Edit catalog

### Skill 1 — `finishing-a-development-branch` (five sites)

**Site 1.1 — Option label (line 57).** Soften Option 2's title to match mship's verb.
- From: `2. Push and create a Pull Request`
- To:   `2. Finish the task and open a Pull Request`

**Site 1.2 — Option 1 "Merge Locally" (lines 70-85).** Keep the existing commands; prepend a one-line note:
> *In a mothership workspace, run `mship close` after the local merge to update state and clean up the worktree.*

**Site 1.3 — Option 2 "Push and Create PR" (lines 91-104).** Replace the fenced block:

```bash
# Write the PR body to a file (or pass inline via --body "...")
cat > /tmp/pr-body.md <<'EOF'
## Summary
<2-3 bullets of what changed>

## Test plan
- [ ] <verification steps>
EOF

# Finish the task: pushes the branch, opens the PR, stamps state.
mship finish --body-file /tmp/pr-body.md
```

**Site 1.4 — Option 4 "Discard" (lines 128-132).** Replace the fenced block:

```bash
mship close --abandon
```

**Site 1.5 — Step 5 "Cleanup Worktree" (lines 136-148).** Replace the body:

> *In a mothership workspace, worktree cleanup is handled by `mship close` (run after merge for Option 1; run after merge notification for Option 2). No manual `git worktree remove` needed.*

Plus: Quick Reference table cell for Option 4 "Cleanup Branch" becomes `✓ (via close --abandon)`.

### Skill 2 — `using-git-worktrees` (A+: top callout + inline substitution)

**Site 2.1 — Top-of-skill callout.** Insert after the `## Overview` section, before `## Directory Selection Process`:

```markdown
## In a mothership workspace

If a `mothership.yaml` is present at any ancestor directory, use `mship spawn '<description>'` instead of the steps below. `mship spawn` creates the worktree, registers it in workspace state, runs per-repo `task setup`, and (where configured) symlinks heavy directories. The Directory Selection, Safety Verification, and Creation Steps sections below apply only when you're not spawning a mship task (e.g., quick read-only exploration or a non-mship repo).
```

**Site 2.2 — Line 97 fenced block.** Leave the `git worktree add` command as-is (the callout above directs mship users elsewhere). Add a `# For non-mship workflows:` comment immediately above the fenced block to reinforce that the snippet is the fallback path, not the primary one.

**Site 2.3 — Example Workflow (lines 180-192).** Replace the example bash with a mship-first variant:

```
You: I'm using the using-git-worktrees skill to set up an isolated workspace.

[Detect: mothership.yaml found at /abs/workspace — routing through mship spawn]
[Run: mship spawn "implement auth middleware" --repos auth-service]
[mship creates worktree at .worktrees/feat/implement-auth-middleware, runs task setup]
[Run: mship test (baseline check)]

Worktree ready at /abs/workspace/.worktrees/feat/implement-auth-middleware
Tests passing (47 tests, 0 failures)
Ready to implement auth middleware
```

### Skill 3 — `writing-plans` (one mship-journal flavor site)

**Site 3.1 — Task template commit step (lines 98-103).** Extend the existing example to pair the commit with a `mship journal` entry, showing plan authors the expected pattern for mship-workspace plans:

From:
```bash
git add tests/path/test.py src/path/file.py
git commit -m "feat: add specific feature"
```

To:
```bash
git add tests/path/test.py src/path/file.py
git commit -m "feat: add specific feature"
# In a mothership workspace, also record the step in the journal:
mship journal "implemented specific feature; tests passing" --action committed
```

Single-sentence note appended to the step caption: *"Pair the commit with a `mship journal` entry so other sessions can reconstruct progress without reading every commit diff."*

### Skill 4 — `executing-plans` (two polish sites)

**Site 4.1 — Step 3 "Complete Development" (lines 32-37).** Extend the existing reference to `finishing-a-development-branch` with a one-liner noting mship's route:

> *In a mothership workspace, `finishing-a-development-branch` routes through `mship finish --body-file <path>` (see that skill's Option 2).*

**Site 4.2 — Promote existing mship bullet from "Remember" list to a dedicated Step 1 subsection.** The current bullet at lines 64-69 is a closing reminder; it should be read before execution begins. Move (don't duplicate) it under `## Step 1: Load and Review Plan` as a numbered sub-item:

```markdown
### Step 1: Load and Review Plan

1. Read plan file
2. Review critically — identify any questions or concerns about the plan
3. If concerns: Raise them with your human partner before starting
4. **If this is a mothership workspace** (`mothership.yaml` at any ancestor): verify `mship status` shows an active task BEFORE starting. No active task → stop and tell the user to `mship spawn "<description>"` first. Then `cd` into `task.worktrees.<repo>` and do all work and commits there. The mship pre-commit hook refuses commits from outside the worktree, so "just commit on main" is both wrong and blocked.
5. If no concerns: Create TodoWrite and proceed
```

Remove the same bullet from the "Remember" list at lines 63-69 to avoid duplication.

## Testing

No unit tests (skills are markdown):

- **Rendering check.** `python -c "import mistune; mistune.html(open(path).read())"` for each edited file — no parse errors.
- **Cross-reference check.** Every `[link](...)` in the edited files resolves to an existing skill in the bundle.
- **Dogfood.** Next `mship finish` + `mship close` in this session exercises Skill 1's updated Option 2 flow; the manual smoke validates no regressions.
- **Install round-trip.** `uv tool install --reinstall --from . mothership` → `mship skill install --only claude --force` → `head -10 ~/.claude/skills/finishing-a-development-branch/SKILL.md` shows the mship flow.

No golden-file test for exact prose; the edit catalog above describes each change precisely enough for PR-time review.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | For `using-git-worktrees`, use A+ (top callout + inline) instead of pure surgical substitution | Pure substitution trivializes the skill's entire Directory Selection + Safety Verification sections. The callout cleanly routes mship users to `mship spawn` while preserving the skill's generic content as-is below. |
| 2 | Keep `writing-plans` in scope despite low conflict | The `git commit` example is illustrative for plan authors, not a workflow prescription. Adding the `mship journal` pairing turns the example into a demonstration of the expected mship plan-authoring pattern — very low cost, nonzero value. |
| 3 | Replace Step 5 of `finishing-a-development-branch` with prose, not code | Worktree cleanup in mship happens as a side effect of `mship close`; users don't run a separate command. Prose conveys that better than a fenced code block. |
| 4 | No conditional forks in prose ("if mship, else"); mship-installed copies assume mship | The skills are distributed by mship; their audience is mship users. Branching makes the prose noisier and doesn't serve anyone. `using-git-worktrees`'s callout is the one exception — it's a short preamble, not a branch scattered through the skill. |
| 5 | Leave ten other skills untouched | No mship conflicts in their current text. Editing them for prose consistency would be gratuitous and risks drift from upstream superpowers. |

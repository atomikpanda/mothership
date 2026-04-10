# `mship finish` PR Creation & Superpowers Skill Design Spec

## Overview

Two features that close the loop on mothership's value chain:

1. **`mship finish` with real PR creation** — creates coordinated PRs across repos in dependency order via `gh` CLI, adds cross-repo coordination blocks, updates cross-references, and stores PR URLs in state.
2. **`working-with-mothership` superpowers skill** — teaches AI agents when and how to use mothership commands, integrating it into the superpowers workflow.

## Feature 1: `mship finish` PR Creation

### Flow

```
mship finish
  → validate: active task exists
  → validate: gh CLI installed and authenticated
  → for each repo in dependency order:
      1. git push branch to remote
      2. gh pr create with title and body
      3. store PR URL in task state
  → update all PRs with coordination block (full cross-references)
  → log PR URLs to task log
  → print summary
```

### PR Title and Body

**Title:** `<task description>` (e.g., "Add labels to tasks")

**Body:** User's task description. For multi-repo tasks, the coordination block is appended after all PRs exist. For single-repo tasks, no coordination block.

### PR Coordination Block

Appended to each PR body after all PRs are created:

```markdown
---

## Cross-repo coordination (mothership)

This PR is part of a coordinated change: `add-labels-to-tasks`

| # | Repo | PR | Merge order |
|---|------|----|-------------|
| 1 | shared | org/shared#18 | merge first |
| 2 | auth-service | org/auth-service#42 | this PR |

⚠ Merge in order — auth-service depends on shared.
```

For single-repo tasks, no coordination block is added (unnecessary).

### State Model Change

Add to `Task` model in `core/state.py`:

```python
class Task(BaseModel):
    # ... existing fields ...
    pr_urls: dict[str, str] = {}  # repo_name → PR URL
```

### Implementation: `core/pr.py`

```python
class PRManager:
    def __init__(self, shell: ShellRunner) -> None: ...
    def check_gh_available(self) -> None: ...
    def push_branch(self, repo_path: Path, branch: str) -> None: ...
    def create_pr(self, repo_path: Path, branch: str, title: str, body: str) -> str: ...
    def update_pr_body(self, pr_url: str, body: str) -> None: ...
    def get_pr_body(self, pr_url: str) -> str: ...
```

All methods delegate to `gh` CLI via `ShellRunner`:
- `check_gh_available`: runs `gh auth status`, raises if not authenticated
- `push_branch`: runs `git push -u origin <branch>` in the repo directory
- `create_pr`: runs `gh pr create --title "..." --body "..." --head <branch>`, returns PR URL
- `update_pr_body`: runs `gh pr edit <url> --body "..."`
- `get_pr_body`: runs `gh pr view <url> --json body -q .body`

### CLI Changes: `cli/worktree.py`

The `finish` command replaces the stub with actual PR creation:

1. Check `gh` availability (`PRManager.check_gh_available()`)
2. For each repo in dependency order:
   - Skip if `task.pr_urls[repo]` already exists (idempotent re-run)
   - Push branch
   - Create PR
   - Store URL in `task.pr_urls`
   - Save state after each PR (crash-safe)
3. Build coordination block with all PR URLs
4. Update each PR body to append the coordination block
5. Log all PR URLs to task log
6. Print summary

### `--handoff` Behavior

Unchanged. `mship finish --handoff` still writes the manifest and returns early without creating PRs.

### Error Handling

- `gh` not installed → error: "Install gh CLI: https://cli.github.com"
- `gh` not authenticated → error: "Run `gh auth login` first"
- `git push` fails (no remote, permission) → error, stop. Already-created PRs remain in state.
- `gh pr create` fails → error, stop. Already-created PRs remain in state.
- Re-running `finish` → skips repos that already have PR URLs, creates only missing ones.
- Single-repo task → creates one PR, no coordination block.

### Post-PR Workflow

After `mship finish`:
- Worktrees and state remain (for post-PR review fixes)
- User pushes fixes to the branch, PR updates automatically
- When done: `mship abort --yes` to clean up worktrees
- Or `mship prune` for orphaned worktrees

### Non-TTY Output

```json
{
  "task": "add-labels-to-tasks",
  "prs": [
    {"repo": "shared", "url": "https://github.com/org/shared/pull/18", "order": 1},
    {"repo": "auth-service", "url": "https://github.com/org/auth-service/pull/42", "order": 2}
  ]
}
```

### DI Container

Add `PRManager` to the container:

```python
pr_manager = providers.Factory(
    PRManager,
    shell=shell,
)
```

## Feature 2: Superpowers Skill — `working-with-mothership`

### Location

```
skills/
  working-with-mothership/
    SKILL.md
```

In the mothership repo, independently installable. Users register the skill in their Claude config by pointing to this directory.

### SKILL.md Structure

**Frontmatter:**
```yaml
---
name: working-with-mothership
description: Use when working in a workspace with mothership.yaml — provides cross-repo coordination, phase-based workflow, and worktree management via the mship CLI
---
```

**Activation:** The skill activates when:
- The current directory (or a parent) contains `mothership.yaml`
- The user mentions multi-repo changes, coordinated worktrees, or cross-service features
- Another skill (brainstorming, writing-plans) is about to begin work that spans repos

**Content sections:**

1. **Overview** — what mothership is, brain vs muscle distinction, how it relates to superpowers
2. **Session Start Protocol** — always run `mship status` then `mship log` at the start of a session to recover context
3. **Phase Workflow** — how phases map to superpowers skills:
   - `plan` phase: brainstorming + writing-plans skills
   - `dev` phase: TDD + subagent-driven-development skills
   - `review` phase: requesting-code-review skill
   - `run` phase: verification-before-completion skill
   - Always call `mship phase <target>` before starting phase work
4. **Command Reference** — when to use each command:
   - `mship init` — setting up a new workspace
   - `mship spawn "description"` — before starting any new task
   - `mship phase <target>` — transitioning between workflow stages
   - `mship test` — before entering review phase
   - `mship block "reason"` / `mship unblock` — when waiting on external input
   - `mship log "message"` — leave breadcrumbs for context recovery
   - `mship finish` — when implementation and review are complete
   - `mship abort --yes` — when abandoning or cleaning up after merge
   - `mship status` — orientation at any time
   - `mship graph` — understanding repo relationships
   - `mship prune` — cleaning up orphaned worktrees
5. **Context Recovery** — what to do when context is lost (session crash, token limit):
   ```bash
   mship status    # what task, what phase, blocked?
   mship log       # what was I doing?
   ```
6. **Single-Repo vs Multi-Repo** — the skill works identically. Single-repo users still benefit from phases, worktree isolation, and context logging.
7. **What NOT to Do** — don't skip phases, don't create worktrees manually (use `mship spawn`), don't forget to `mship log` progress, don't merge PRs out of the order shown by `mship finish`

### Prerequisite

Assumes superpowers is installed. References superpowers skills by name (brainstorming, TDD, etc.) but does not depend on them at runtime — mothership commands work independently.

### What the Skill Does NOT Do

- Does not replace any superpowers skill — purely additive
- Does not define its own methodology (brainstorming, TDD, etc.)
- Does not execute mothership commands itself — guides the agent on when to call `mship`

## Files Changed/Created

### Feature 1: `mship finish`

| File | Change | Purpose |
|------|--------|---------|
| `src/mship/core/state.py` | Modify | Add `pr_urls` field to Task model |
| `src/mship/core/pr.py` | Create | PRManager: gh CLI integration |
| `src/mship/cli/worktree.py` | Modify | Replace finish stub with PR creation |
| `src/mship/container.py` | Modify | Add PRManager provider |
| `tests/core/test_pr.py` | Create | PRManager tests |
| `tests/core/test_state.py` | Modify | Test pr_urls field |
| `tests/cli/test_worktree.py` | Modify | Test finish with PR creation |

### Feature 2: Skill

| File | Change | Purpose |
|------|--------|---------|
| `skills/working-with-mothership/SKILL.md` | Create | Agent guidance skill |

---
name: working-with-mothership
description: Use when working in a workspace with mothership.yaml — provides cross-repo coordination, phase-based workflow, and worktree management via the mship CLI
---

# Working with Mothership

## Overview

Mothership (`mship`) is a CLI tool that provides phase-based workflow orchestration, coordinated worktree management, and structured task execution. It works for single-repo and multi-repo workspaces.

**You are the brain. Mothership is the coordinator. go-task is the muscle.**

- You decide what to build and how to build it
- Mothership tracks phases, manages worktrees, coordinates PRs across repos
- go-task (via Taskfile.yml per repo) runs the actual build/test/lint commands

**Announce at start:** "I'm using the working-with-mothership skill for workspace coordination."

## Session Start Protocol

**Every session, before doing anything else:**

```bash
mship status    # What task am I on? What phase? Am I blocked?
mship log       # What was I doing before this session?
```

If `mship status` fails with "No mothership.yaml found", you're not in a mothership workspace. Skip this skill.

If there's no active task, ask the user what they want to work on, then `mship spawn`.

## Phase Workflow

Mothership enforces a phase progression. Always transition phases explicitly.

| Phase | What happens | Superpowers skill |
|-------|-------------|-------------------|
| `plan` | Brainstorm, write spec, create plan | brainstorming, writing-plans |
| `dev` | Implement the plan, write tests, commit | test-driven-development, subagent-driven-development |
| `review` | Review code, run full test suite | requesting-code-review, verification-before-completion |
| `run` | Deploy, run services, verify in environment | verification-before-completion |

**Transition with:** `mship phase <target>`

Mothership warns (but doesn't block) if preconditions aren't met:
- Entering `dev` without a spec → warning
- Entering `review` without passing tests → warning
- Entering `run` with uncommitted changes → warning

**Respect the warnings.** If mothership warns about missing tests, run `mship test` before proceeding.

## Command Reference

### Starting work

```bash
mship init                          # First time: set up workspace (interactive)
mship init --name my-app --repo ./:service  # Non-interactive setup
mship spawn "add user avatars"      # Create worktrees for a new task
mship spawn "fix auth" --repos shared,auth-service  # Specific repos only
```

### During work

```bash
mship phase dev                     # Transition to development phase
mship test                          # Run tests across repos (dependency order)
mship test --all                    # Run all even if one fails
mship log "refactored auth controller, tests passing"  # Leave breadcrumbs
mship status                        # Check current state
```

### When blocked

```bash
mship block "waiting on API key from ops team"  # Park the task
mship unblock                       # Resume when unblocked
```

### Finishing work

```bash
mship phase review                  # Move to review phase
mship test                          # Verify all tests pass
mship finish                        # Create coordinated PRs
mship abort --yes                   # Clean up worktrees after merge
```

### Workspace awareness

```bash
mship graph                         # Show repo dependency graph
mship prune                         # Find orphaned worktrees (dry-run)
mship prune --force                 # Clean up orphaned worktrees
```

## Context Recovery

When your context is wiped (new session, crash, token limit):

1. Run `mship status` — tells you the task, phase, repos, test results, and blocked state
2. Run `mship log` — tells you the narrative of what you were doing
3. Run `mship log --last 3` — just the recent entries if the log is long

**Always log your progress** before ending a session or when you've completed a significant step. Future you (or another agent) will thank you.

## Integration with Superpowers

This skill is additive — it coordinates superpowers skills, not replaces them.

**Before brainstorming:** `mship spawn "description"` → `mship phase plan`
**Before implementing:** `mship phase dev` → use TDD skill
**Before reviewing:** `mship test` → `mship phase review` → use code-review skill
**Before finishing:** `mship finish` → creates PRs with merge order

## Single-Repo vs Multi-Repo

Everything works the same. In a single-repo workspace:
- `mship spawn` creates one worktree
- `mship test` runs tests in one repo
- `mship finish` creates one PR (no coordination block needed)
- Phases, logging, and blocked state work identically

## What NOT to Do

- **Don't skip phases** — follow plan → dev → review → run
- **Don't create worktrees manually** — use `mship spawn`
- **Don't forget to log** — `mship log "what I did"` after significant work
- **Don't merge PRs out of order** — follow the merge order in the PR coordination block
- **Don't ignore soft gate warnings** — they exist for a reason
- **Don't run `mship finish` without passing tests** — run `mship test` first

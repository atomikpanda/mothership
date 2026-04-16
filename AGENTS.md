# Working in this repo

This repo is its own product — dogfood it.

## Use `mship` for every task

Before editing files, start a task in an isolated worktree:

```
mship spawn '<short description>' --repos mothership
cd <printed worktree path>
```

Don't edit `main` directly. You lose state isolation, skip the audit/reconcile
gates, and don't exercise the workflow this repo exists to enable. Single-repo
is supported — `--repos mothership` is the whole invocation.

## Read the skills first

Workflow specifics live in `skills/`, not here. The relevant ones:

- `skills/working-with-mothership/SKILL.md` — the canonical mship workflow
  (session start, phases, switch/finish/close, recovery)
- `skills/using-git-worktrees/SKILL.md` — worktree mechanics
- `skills/finishing-a-development-branch/SKILL.md` — finish → PR flow
- `skills/verification-before-completion/SKILL.md` — what "done" means

If you're touching this codebase and haven't read `working-with-mothership`,
stop and read it.

## Bypass flags

Safety overrides use `--bypass-<check>` (e.g. `--bypass-reconcile`,
`--bypass-base-ancestry`), not `--force-<check>`. `--force` is the
all-bypass nuclear option; prefer the targeted flag.

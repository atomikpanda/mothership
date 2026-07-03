# Working in this repo

This repo is its own product — dogfood it.

## Use `mship` for every task

Before editing files, start a task in an isolated worktree. Every task needs a
WorkItem — create one, then spawn against it:

```
mship item new '<title>' --kind <feature|bug|chore|question>   # prints wi-<id>
mship spawn '<short description>' --work-item <id> --repos mothership
cd <printed worktree path>
```

`spawn` refuses without `--work-item` (`--hotfix` overrides in emergencies,
logged). **feature**-kind work also needs an **approved spec** before
`mship phase dev`/`finish`; **bug/chore/question** need only the WorkItem.

Don't edit `main` directly. You lose state isolation, skip the audit/reconcile
gates, and don't exercise the workflow this repo exists to enable. Single-repo
is supported — `--repos mothership` is the whole invocation.

## Read the skills first

Workflow specifics live in `skills/`, not here. The relevant ones:

- `skills/working-with-mothership/SKILL.md` — the canonical mship workflow
  (session start, phases, switch/finish/close, recovery, task dependencies)
- `skills/using-git-worktrees/SKILL.md` — worktree mechanics
- `skills/finishing-a-development-branch/SKILL.md` — finish → PR flow
- `skills/verification-before-completion/SKILL.md` — what "done" means

If you're touching this codebase and haven't read `working-with-mothership`,
stop and read it.

## Bypass flags

Safety overrides use `--bypass-<check>` (e.g. `--bypass-reconcile`,
`--bypass-base-ancestry`, `--bypass-deps`), not `--force-<check>`. `--force` is the
all-bypass nuclear option; prefer the targeted flag.

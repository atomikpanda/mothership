# Ship a feature

**When you need this:** you're building something new that deserves a design —
you want the *what and why* agreed before code exists.

## The loop at a glance

```
work item (feature) → spec → review + approve → plan → build → finish
```

Feature work items pass through mship's design gates before development; the
[Concepts page](../concepts.md#the-lifecycle) has the full lifecycle diagram.
Bugs and chores skip the design gates — that faster loop is
[Fix a bug](fix-a-bug.md).

## 1. Create the work item

```bash
mship item new "Search across projects" --kind feature
# wi-. . . created
```

## 2. Write the spec

A **spec** is the approved design: problem, user story, approach, acceptance
criteria. It lives at `specs/<date>-<id>.md` in the workspace.

```bash
mship spec new --title "Search across projects" --id search-across-projects
mship spec draft search-across-projects
```

`spec draft` prints a drafting prompt — run it through your agent (or fill the
JSON yourself), then apply the result:

```bash
cat draft.json | mship spec apply search-across-projects --from-json -
# status: needs_review
```

Link it to the work item if it isn't already:

```bash
mship item link-spec <wi-id> search-across-projects
```

## 3. Review and approve

```bash
mship spec review search-across-projects    # criteria + open questions, one unit at a time
mship spec approve search-across-projects   # gate: all criteria approved, all questions answered
```

Prefer the phone? Specs in `needs_review` surface in Ground Control's Queue for
one-tap approval — see [Phone control](phone-control.md). To send one back:
`mship spec request-changes <id> --reason "..."`.

## 4. Plan

Features need an implementation plan before development — a bite-sized,
TDD-oriented breakdown at `<docs_dir>/plans/<date>-<slug>.md`, with each task
wrapped in `mship:task` anchors so it can be handed to an agent one task at a
time. The `writing-plans` skill (installed via `mship skill install`) is the
canonical way to produce one. Link it:

```bash
mship item link-plan <wi-id> docs/plans/2026-07-25-search-across-projects.md
```

## 5. Build

Two equivalent entry points:

```bash
mship spec dispatch search-across-projects   # binds the approved spec to a new task + emits a handoff
# or
mship spawn "search across projects" --work-item <wi-id>
```

Then move the task into development:

```bash
mship phase dev
```

`phase dev` is where the three gates fire: the task must belong to a work item,
a feature's spec must be approved, and its plan must exist. If a gate blocks
you, it says exactly which link is missing
([Concepts → the three gates](../concepts.md#the-three-gates)).

Build inside the task's worktrees as usual — edit, `mship test`,
`mship journal` as you go. Working with an agent?
[Agent-driven development](agent-driven-development.md) covers dispatching plan
tasks to subagents.

## 6. Finish

```bash
mship finish
```

The same gates run at finish. The PR body carries the spec's acceptance
criteria, so reviewers see what the change promises. After the merge:
`mship close`.

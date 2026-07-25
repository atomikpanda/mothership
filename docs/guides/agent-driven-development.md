# Agent-driven development

**When you need this:** an AI coding agent does the building, and you want it
operating safely inside the workspace instead of improvising with raw git.

## Install the skills

```bash
mship skill install
```

This installs the mship skill bundle for Claude Code, including
**`working-with-mothership`** — the canonical operating guide an agent loads
when it works in an mship workspace: how to resolve the active task, when to
journal, how phases and gates behave, how to finish.

## What the guardrails give you

An agent in an mship workspace can't rationalize its way into the classic
failure modes, because the boundaries are enforced, not suggested
([Concepts](../concepts.md#the-three-gates)):

- **It can't edit or commit to `main`** while a task is active — the pre-commit
  hook and the editor guard refuse, pointing at the task's worktree instead.
- **It can't ship undesigned features** — `phase dev` and `finish` check the
  work item, the approved spec, and the plan for feature work.
- **It can't lose the thread** — `mship status` / `mship context` give it
  structured state instead of shell archaeology, and the journal records what
  happened for the next session.

## Orchestrator + subagents

The pattern that scales: one **orchestrator** session owns the task — spec,
plan, integration, `mship finish` — and dispatches fresh **subagents** to
implement one plan task each:

```bash
mship dispatch --task <slug> --plan-task 3
```

This prints a self-contained prompt: which worktree to `cd` into, the branch
state, recent journal lines, and exactly one plan task — so the subagent needs
no inherited context. Between subagents, the orchestrator reviews (spec
compliance first, code quality second) before dispatching the next.

Two rules of thumb:

- **Parallel subagents are safe to run.** Both halves are isolated: each task
  gets its own worktree and branch, so code edits never collide, and every write
  to `state.yaml` goes through a single read-modify-write under an exclusive
  `flock` with an atomic replace — so concurrent `journal` / `test` / `finish`
  calls cannot lose each other's updates. `mship journal` writes one file per
  task in append mode, and test results are recorded inside the same locked
  mutation. There are multiprocessing regression tests covering concurrent
  phase transitions and a same-slug spawn race.

  What actually limits how many agents you can run is elsewhere: **one inbox
  listener per workspace** (the mailbox lease refuses a second, so only one
  agent per workspace can hear the phone), **one `mship serve` per workspace**,
  the **review loop** — every feature needs an approved spec, and Ground
  Control's queue is one card at a time — and **CPU for the test suite** when
  several agents run it at once. Scale by adding workspaces, or by keeping one
  orchestrator per workspace that fans work out to subagents.
- **Commit early, journal always.** `mship journal "<what happened>"` after
  each meaningful step is what lets any future session — human or agent —
  reconstruct the work without replaying it.

## Sharing state with another session

```bash
mship export            # bundle the task: journal, plan, spec, diffs
mship export --redacted # same, with opt-in secret redaction
```

Hand the bundle to a reviewer, a teammate, or another agent — everything needed
to evaluate or continue the task, in one artifact.

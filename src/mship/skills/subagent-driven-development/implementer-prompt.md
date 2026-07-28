# Implementer Subagent Prompt Template

Use this template when dispatching an implementer subagent.

**IMPORTANT — before dispatching (mothership workspace):** If a
`mothership.yaml` exists at the repo root or any ancestor:

1. There MUST be a single, anchored mship task. Run `mship status` — it
   always returns an envelope shape:
   - If `.active_tasks` is empty (`mship status | jq '.active_tasks'` → `[]`),
     refuse to dispatch. Every task needs a WorkItem: tell the user to run
     `mship item new "<title>" --kind <feature|bug|chore|question>` then
     `mship spawn "<description>" --work-item <id>` first.
   - If `.active_tasks` is non-empty but `.resolved_task` is `null`, multiple
     tasks are active with no anchor — refuse to dispatch, pick one with the
     user, then set `MSHIP_TASK=<slug>` (or pass `--task <slug>`) and re-run
     `mship status` to confirm `.resolved_task` is populated.
   - Otherwise `.resolved_task` is the resolved task's detail (`slug`, `phase`,
     `worktrees`, …). Use that for step 2.
2. Run `mship dispatch --task <slug> --plan-task <N>` and read the **stub** it
   prints: record path, resolved model, mode, worktree. The stub's `worktree`
   is the subagent's cwd and the stub's `model` fills `[MODEL]` below — the
   model was resolved by the CLI (`--model` > `dispatch_models` config >
   per-mode default); never let the worker choose, and never substitute your
   session's model.
3. The subagent MUST work in the task's worktree, not the main checkout, and
   MUST commit on the task's feature branch. The mship pre-commit hook will
   refuse commits from the main checkout, but the prompt says this explicitly
   so the subagent doesn't waste a cycle.

If this is NOT a mothership workspace, point `Work from:` at the project's
worktree, pick the model per SKILL.md Model Selection, and replace the
`mship dispatch --emit` step with an inline task brief.

```
Subagent (general-purpose):
  description: "Implement Task N: [task name]"
  model: [MODEL — from the dispatch stub's resolved model line; an omitted
         model silently inherits the session's most expensive one]
  prompt: |
    You are implementing one plan task for the mship task [slug].

    ## Your Task

    Work from: [worktree path from the stub, NOT the main repo root]

    Your FIRST command, from that directory:

        mship dispatch --emit

    It prints your full assignment — the plan task's text, acceptance
    criteria, worktree, phase, recent journal, and base SHAs. That emitted
    prompt is your requirements, with the exact values to use verbatim.
    Heed any drift warnings it prints to stderr.

    ## Context

    [Scene-setting the emit cannot know: interfaces and decisions from
    earlier tasks, your resolution of any ambiguity, pointers to parked
    journal findings in this area]

    ## Before You Begin

    If you have questions about:
    - The requirements or acceptance criteria
    - The approach or implementation strategy
    - Dependencies or assumptions
    - Anything unclear in the task description

    **Ask them now.** Raise any concerns before starting work.

    ## Your Job

    Once you're clear on requirements:
    1. Implement exactly what the task specifies
    2. Write tests (following TDD if task says to)
    3. Verify implementation works — run `mship test` (not a bare runner
       like `pytest`) so `mship finish` keeps the test-evidence trail
    4. Commit your work on the current feature branch (see "Where to work")
    5. Self-review (see below)
    6. Report back

    ## Where to work

    - Stay inside the worktree above for ALL edits and commits. `cd` there
      at the start if your shell isn't already in it.
    - The worktree is checked out on the task's feature branch (e.g.
      `feat/<task-slug>`). `git status` should show that branch, not `main`.
    - **Never commit to `main`.** If you find yourself on `main`, stop and
      report back as `BLOCKED` — the controller set up the wrong directory.
      The mship pre-commit hook will refuse anyway, but don't waste a cycle.
    - Don't run `git checkout -b` yourself. The branch already exists.

    **While you work:** If you encounter something unexpected or unclear, **ask questions**.
    It's always OK to pause and clarify. Don't guess or make assumptions.

    While iterating, run the focused test for what you're changing; run
    `mship test` once before committing, not after every edit.

    ## Code Organization

    You reason best about code you can hold in context at once, and your edits are more
    reliable when files are focused. Keep this in mind:
    - Follow the file structure defined in the plan
    - Each file should have one clear responsibility with a well-defined interface
    - If a file you're creating is growing beyond the plan's intent, stop and report
      it as DONE_WITH_CONCERNS — don't split files on your own without plan guidance
    - If an existing file you're modifying is already large or tangled, work carefully
      and note it as a concern in your report
    - In existing codebases, follow established patterns. Improve code you're touching
      the way a good developer would, but don't restructure things outside your task.

    ## When You're in Over Your Head

    It is always OK to stop and say "this is too hard for me." Bad work is worse than
    no work. You will not be penalized for escalating.

    **STOP and escalate when:**
    - The task requires architectural decisions with multiple valid approaches
    - You need to understand code beyond what was provided and can't find clarity
    - You feel uncertain about whether your approach is correct
    - The task involves restructuring existing code in ways the plan didn't anticipate
    - You've been reading file after file trying to understand the system without progress

    **How to escalate:** Report back with status BLOCKED or NEEDS_CONTEXT. Describe
    specifically what you're stuck on, what you've tried, and what kind of help you need.
    The controller can provide more context, re-dispatch with a more capable model,
    or break the task into smaller pieces.

    ## Before Reporting Back: Self-Review

    Review your work with fresh eyes. Ask yourself:

    **Completeness:**
    - Did I fully implement everything in the spec?
    - Did I miss any requirements?
    - Are there edge cases I didn't handle?

    **Quality:**
    - Is this my best work?
    - Are names clear and accurate (match what things do, not how they work)?
    - Is the code clean and maintainable?

    **Discipline:**
    - Did I avoid overbuilding (YAGNI)?
    - Did I only build what was requested?
    - Did I follow existing patterns in the codebase?

    **Testing:**
    - Do tests actually verify behavior (not just mock behavior)?
    - Did I follow TDD if required?
    - Are tests comprehensive?
    - Is the test output pristine (no stray warnings or noise)?

    If you find issues during self-review, fix them now before reporting.

    ## After Review Findings

    If the task review finds issues, you will be resumed with the findings.
    Fix them, re-run the tests that cover the amended code (`mship test`
    before committing), and append a fix report to your report file: what
    you changed, the covering tests you ran, the command, and the output.
    Reviewers will not re-run tests for you — your report is the test
    evidence. Then reply with the same short status contract as your first
    report.

    ## Report Format

    Write your full report to [REPORT_FILE]:
    - What you implemented (or what you attempted, if blocked)
    - What you tested and test results
    - **TDD Evidence** (if TDD was required for this task):
      - RED: command run, relevant failing output before implementation, and why the failure was expected
      - GREEN: command run and relevant passing output after implementation
    - Files changed
    - Self-review findings (if any)
    - Any issues or concerns

    Then report back with ONLY (under 15 lines — the detail lives in the
    report file):
    - **Status:** DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT
    - Commits created (short SHA + subject)
    - One-line test summary (e.g. "mship test green, 14/14, output pristine")
    - Your concerns, if any
    - The report file path

    If BLOCKED or NEEDS_CONTEXT, put the specifics in the final message
    itself — the controller acts on it directly.

    Use DONE_WITH_CONCERNS if you completed the work but have doubts about correctness.
    Use BLOCKED if you cannot complete the task. Use NEEDS_CONTEXT if you need
    information that wasn't provided. Never silently produce work you're unsure about.
```

**Placeholders:**
- `[MODEL]` — the resolved model line from the dispatch stub (never your
  session's model; outside mothership, choose per SKILL.md Model Selection)
- `[slug]` — the task slug from the stub
- `[REPORT_FILE]` — the report path the controller names next to the
  dispatch record the stub printed (record `…/record.json` →
  `…/task-N-report.md`)

# Task Reviewer Prompt Template

Use this template when dispatching a task reviewer subagent. The reviewer
reads the task's review package once and returns two verdicts: spec
compliance and code quality.

**Purpose:** Verify one task's implementation matches its requirements (nothing
more, nothing less) and is well-built (clean, tested, maintainable)

**Before dispatching:** run `mship dispatch --mode reviewer --task <slug>`.
It builds the review package (one raw diff file per affected repo +
`manifest.json`, diffed from the prior dispatch's recorded base to live HEAD)
under the dispatch record's `review/` directory and prints a closed stub.
The stub's `worktree` is the reviewer's cwd and the stub's `model` fills
`[MODEL]` below. The reviewer's own `mship dispatch --emit` prints the
diff-file paths, the manifest path, the live acceptance criteria, the
skipped-repo disclosure (if any), and the read-only dual-verdict contract —
this template adds only what the CLI cannot know.

```
Subagent (general-purpose):
  description: "Review Task N (spec + quality)"
  model: [MODEL — from the reviewer stub's resolved model line; an omitted
         model silently inherits the session's most expensive one]
  prompt: |
    You are reviewing one task's implementation: first whether it matches its
    requirements, then whether it is well-built. This is a task-scoped gate,
    not a merge review — a broad whole-branch review happens separately after
    all tasks are complete.

    Work from: [worktree path from the reviewer stub]

    Your FIRST command, from that directory:

        mship dispatch --emit

    It prints your review package: the diff-file paths and manifest on disk,
    the acceptance criteria to check (live from the spec store), and your
    read-only dual-verdict contract. That contract governs; everything below
    supplements it.

    ## What Was Requested

    Read the plan task's anchored block — and only that block — in
    [PLAN_FILE] (anchor `<!-- mship:task id=N -->`). It is the requirements,
    with the exact values to use verbatim. Do not read the rest of the plan.

    Global constraints from the spec/design that bind this task:
    [GLOBAL_CONSTRAINTS]

    ## What the Implementer Claims They Built

    Read the implementer's report: [REPORT_FILE]

    ## Diff Under Review

    Read each diff file the emit listed once — together they are your view
    of the change: the full per-repo diffs with surrounding context. The
    diffs' context lines ARE the changed files: do not Read a changed file
    separately unless a hunk you must judge is cut off mid-function — and
    say so in your report. Do not re-run git commands. If the emit lists
    repos under "Affected repos NOT included in this package", honor that
    disclosure: your verdict cannot cover them — mark any acceptance
    criterion touching them as can't-tell and state the omission in your
    report. Do not crawl the broader codebase. Inspect code outside the diff
    only to evaluate a concrete risk you can name — one focused check per
    named risk, and name both the risk and what you checked in your report.
    Cross-cutting changes are legitimate named risks: if the diff changes
    lock ordering, a function or API contract, or shared mutable state,
    checking the call sites is the right method.

    Your review is read-only on this checkout. Do not mutate the working
    tree, the index, HEAD, or branch state in any way.

    ## Do Not Trust the Report

    Treat the implementer's report as unverified claims about the code. It
    may be incomplete, inaccurate, or optimistic. Verify the claims against
    the diff. Design rationales in the report are claims too: "left it per
    YAGNI," "kept it simple deliberately," or any other justification is the
    implementer grading their own work. Judge the code on its merits — a
    stated rationale never downgrades a finding's severity.

    ## Tests

    The implementer already ran `mship test` and reported results with TDD
    evidence for exactly this code. Do not re-run the suite to confirm their
    report. Run a test only when reading the code raises a specific doubt
    that no existing run answers — and then a focused test, never a
    package-wide suite, race detector run, or repeated/high-count loop. If
    heavy validation seems warranted, recommend it in your report instead of
    running it. If you cannot run commands in this environment, name the
    test you would run.

    Warnings or other noise in the implementer's reported test output are
    findings — test output should be pristine.

    ## Part 1: Spec Compliance

    Compare the diff against What Was Requested and the emitted acceptance
    criteria:

    - **Missing:** requirements they skipped, missed, or claimed without
      implementing
    - **Extra:** features that weren't requested, over-engineering, unneeded
      "nice to haves"
    - **Misunderstood:** right feature built the wrong way, wrong problem
      solved

    If a requirement cannot be verified from this diff alone (it lives in
    unchanged code, spans tasks, or touches a skipped repo), report it as a
    ⚠️ item instead of broadening your search.

    ## Part 2: Code Quality

    **Code quality:**
    - Clean separation of concerns?
    - Proper error handling?
    - DRY without premature abstraction?
    - Edge cases handled?

    **Tests:**
    - Do the new and changed tests verify real behavior, not mocks?
    - Are the task's edge cases covered?

    **Structure:**
    - Does each file have one clear responsibility with a well-defined interface?
    - Are units decomposed so they can be understood and tested independently?
    - Is the implementation following the file structure from the plan?
    - Did this change create new files that are already large, or
      significantly grow existing files? (Don't flag pre-existing file
      sizes — focus on what this change contributed.)

    Your report should point at evidence: file:line references for every
    finding and for any check you would otherwise answer with a bare
    "yes." A tight report that cites lines gives the controller everything
    it needs.

    Your final message is the report itself: begin directly with the
    spec-compliance verdict. Every line is a verdict, a finding with
    file:line, or a check you ran — no preamble, no process narration,
    no closing summary.

    ## Calibration

    Categorize issues by actual severity. Not everything is Critical.
    Important means this task cannot be trusted until it is fixed: incorrect
    or fragile behavior, a missed requirement, or maintainability damage you
    would block a merge over — verbatim duplication of a logic block,
    swallowed errors, tests that assert nothing. "Coverage could be broader"
    and polish suggestions are Minor.
    If the plan or brief explicitly mandates something this rubric calls a
    defect (a test that asserts nothing, verbatim duplication of a logic
    block), that IS a finding — report it as Important, labeled
    plan-mandated. The plan's authorship does not grade its own work; the
    human decides.
    Acknowledge what was done well before listing issues — accurate praise
    helps the implementer trust the rest of the feedback.

    ## Output Format

    ### Spec Compliance

    - ✅ Spec compliant | ❌ Issues found: [what's missing/extra/misunderstood,
      with file:line references]
    - ⚠️ Cannot verify from diff: [requirements you could not verify from the
      diff alone — including skipped repos — and what the controller should
      check; report alongside the ✅/❌ verdict for everything you could verify]

    ### Strengths
    [What's well done? Be specific.]

    ### Issues

    #### Critical (Must Fix)
    #### Important (Should Fix)
    #### Minor (Nice to Have)

    For each issue: file:line, what's wrong, why it matters, how to fix
    (if not obvious).

    ### Assessment

    **Task quality:** [Approved | Needs fixes]

    **Reasoning:** [1-2 sentence technical assessment]
```

**Placeholders:**
- `[MODEL]` — the resolved model line from the reviewer stub (`mship dispatch
  --mode reviewer` resolves it: `--model` > `dispatch_models` config >
  per-mode default)
- `[PLAN_FILE]` — the plan file path; the reviewer reads only Task N's
  anchored block (the same text the implementer's emit delivered)
- `[GLOBAL_CONSTRAINTS]` — the binding requirements copied verbatim from
  the plan's Global Constraints section or the spec: exact values, formats,
  and stated relationships between components (not process rules — those
  are already in this template)
- `[REPORT_FILE]` — REQUIRED: the file the implementer wrote its detailed
  report to (next to the dispatch record)

The diff files, manifest, base/head SHAs, acceptance criteria, and
skipped-repo disclosure all come from the reviewer's own
`mship dispatch --emit` — never paste diff content into the prompt.

**Reviewer returns:** Spec Compliance verdict (✅/❌/⚠️), Strengths, Issues
(Critical/Important/Minor), Task quality verdict

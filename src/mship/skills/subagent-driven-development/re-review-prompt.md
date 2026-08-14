# Scoped Re-Review Prompt Template

Use this template when dispatching a re-review after a fix round. The
re-reviewer verifies the findings were addressed and checks the fix diff for
new breakage. It is not a fresh review — the full review already happened.

**Purpose:** Verify each finding from the previous review was addressed, and
that the fix itself broke nothing.

**Before dispatching:** re-run `mship dispatch --mode reviewer --task <slug>`.
The rebuilt package diffs to the new HEAD, so it contains the whole task's
diff including the fix commits; this template scopes the re-reviewer to the
fix range `[FIX_BASE_SHA]..[HEAD_SHA]` within it. The stub's `worktree` is
the re-reviewer's cwd.

Read the stub's resolved model before dispatch:
- `inherit`: omit the harness model selector; the harness default is intended.
- any other value: pass it unchanged through a supported model selector.
- if the available subagent API has no model selector, do not dispatch with
  an explicit value. Report: "mship resolved explicit model '<value>', but this subagent API cannot select a model; set this mode to inherit or use a selector-capable dispatch tool."
Never translate one provider's model name into another.

The Task-style template below shows `model: vendor/custom-tier` for an explicit
resolved value. Replace it with that exact value. For `inherit`, omit the entire
selector field; do not pass the literal string `inherit` to the provider.

```
Subagent (general-purpose):
  description: "Re-review Task N fix round R"
  model: vendor/custom-tier
  prompt: |
    You are re-reviewing one task's fix round. A previous review produced
    findings; an implementer has attempted to fix them. Your job is to
    verdict each finding and inspect the fix diff — nothing else.

    Work from: [worktree path from the reviewer stub]

    Your FIRST command, from that directory:

        mship dispatch --emit

    It prints the review package: diff-file paths and manifest on disk,
    acceptance criteria, and the read-only contract. The package spans the
    whole task; YOUR scope is narrower — the fix range below.

    ## The Findings Under Verification

    [FINDINGS]

    ## The Fix

    Read the implementer's report (fix reports are appended at the end):
    [REPORT_FILE]

    **Fix base:** [FIX_BASE_SHA] (the head the previous review saw)
    **Head:** [HEAD_SHA]

    The package's diff files run from the task base to head; the fix commits
    are the hunks after [FIX_BASE_SHA]. Identify the fix range from the
    commit list (`git log --oneline [FIX_BASE_SHA]..[HEAD_SHA]`, read-only)
    and judge only those changes. Do not re-run other git commands. Honor
    any "Affected repos NOT included in this package" disclosure the emit
    printed: findings in those repos are can't-tell, stated in your report.

    Your review is read-only on this checkout. Do not mutate the working
    tree, the index, HEAD, or branch state in any way.

    ## Scope

    Your scope is the findings list and the fix diff. Verdict every finding.
    Inspect the fix diff for new problems the fix itself introduced. Do NOT
    re-review code the fix did not touch: if you notice an issue entirely
    outside the fix diff, report it under Out-of-Scope Observations — it
    does not block this task and does not extend the loop. A broad
    whole-branch review happens after all tasks are complete.

    ## Tests

    The implementer re-ran the tests covering the amended code (`mship
    test` before committing) and appended the results to the report file.
    Treat the report as unverified claims: confirm the fix report names the
    covering tests and shows their output, and verify the claims against
    the diff. Do not re-run the suite to confirm their report. Run a test
    only when reading the code raises a specific doubt that no existing run
    answers — and then a focused test, never a package-wide suite.

    ## Output Format

    Your final message is the report itself: begin directly with the first
    finding's verdict. Every line is a verdict, a finding with file:line,
    or a check you ran — no preamble, no process narration.

    ### Finding Verdicts

    For each finding in The Findings Under Verification, in order:
    - **[finding one-liner]** — ADDRESSED | NOT ADDRESSED, with file:line
      evidence. "Attempted" is not addressed: the specific defect must no
      longer exist.

    ### New Breakage in the Fix Diff

    Anything the fix itself broke or introduced, with severity
    (Critical/Important/Minor) and file:line. "None" if clean.

    ### Out-of-Scope Observations

    Issues you noticed entirely outside the fix diff. Non-blocking; the
    controller journals these for the final review. "None" if none.

    ### Verdict

    **Fix round:** [All findings addressed, no new Critical/Important
    breakage | Findings remain open] — list the open ones.
```

**Placeholders:**
- `vendor/custom-tier` — an example explicit model value; replace it unchanged
  with the resolved value, or omit the entire selector field for `inherit`
- `[FINDINGS]` — the Critical/Important findings and spec gaps from the
  previous review, copied verbatim, one per bullet
- `[REPORT_FILE]` — the implementer's report file (fix reports appended)
- `[FIX_BASE_SHA]` — the head the previous review saw
- `[HEAD_SHA]` — current commit

The diff files and manifest come from the re-run of
`mship dispatch --mode reviewer` via the re-reviewer's own
`mship dispatch --emit` — never paste diff content into the prompt.

**Re-reviewer returns:** per-finding verdicts (ADDRESSED / NOT ADDRESSED),
new breakage in the fix diff, out-of-scope observations, and a round verdict.

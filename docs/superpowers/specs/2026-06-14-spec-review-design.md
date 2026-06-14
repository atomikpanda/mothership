# `mship spec review` + `spec verdict` — design (MOS-147 / A3)

> Status: design approved 2026-06-14. Feeds `writing-plans`.
> Part of **Ground Control** (epic MOS-144). Builds on A1 (Spec substrate, #166)
> and A2 (draft/apply, #167).

## Context

A2's `apply` populates `acceptance_criteria` (each with a stable `ac<n>` id and a
`verdict` defaulting to `unreviewed`) and `open_questions`. A3 surfaces those as
**review units** for Ground Control's review cards (C4) and lets a reviewer
record a verdict per criterion. The `Spec` model already stores
`AcceptanceCriterion.verdict ∈ {unreviewed, approved, flagged}` — A3 adds no model
fields.

## Decisions

1. **Criteria-only verdicts; prose is read-only context.** Verdicts live only on
   `acceptance_criteria`. `spec review` still *emits* Problem / User story /
   Approach / Non-goals / Risks as read-only context (so the C4 cards have their
   content), but those are not individually Approve/Flag-able in A3. Per-prose-card
   verdicts are a small follow-on if C4 needs them.
2. **Two commands, no status transition.** `spec review` emits; `spec verdict`
   records one criterion verdict. Neither changes `Spec.status` — that's A5
   (`approve`). Recording a verdict is a pure annotation (ungated by status).
3. **Strictly factual.** Output quotes the spec verbatim; no inference, no
   "recommended action."

## Commands

### `mship spec review <id> [--json]`
Load the spec and emit its review units. JSON shape (TTY renders a readable
version of the same):

```json
{
  "id": "decision-queue",
  "status": "needs_review",
  "acceptance_criteria": [
    {"id": "ac1", "text": "User can view blocking questions", "verdict": "approved"},
    {"id": "ac2", "text": "Answer is recorded in the journal", "verdict": "unreviewed"}
  ],
  "open_questions": [
    {"id": "q1", "text": "Android in v0?", "answer": null}
  ],
  "context": {
    "problem": "…", "user_story": "…", "approach": "…",
    "non_goals": ["…"], "risks": ["…"], "affected_repos": ["…"]
  },
  "summary": {
    "criteria_total": 2, "approved": 1, "flagged": 0, "unreviewed": 1,
    "open_questions_unanswered": 1
  }
}
```
`context.problem/user_story/approach` come from `parse_body_sections(spec.body)`
(A2's helper); `non_goals`/`risks`/`affected_repos` from the frontmatter.

### `mship spec verdict <id> <criterion-id> <verdict>`
Record a verdict on one acceptance criterion.
- `verdict ∈ {unreviewed, approved, flagged}` — else error listing the valid set.
- `criterion-id` must match an `acceptance_criteria[].id` — else error listing the
  valid ids. (A prose unit id like `problem` → error: "not verdict-able; only
  acceptance criteria carry verdicts in this version.")
- Sets the criterion's `verdict`, bumps `updated_at`, saves via `SpecStore`.
- No status transition; ungated by status (a verdict is an annotation).

## Scope (A3 only)

`spec review` (emit) + `spec verdict` (record) + the small render/summarize helper.
**Out of scope:** `spec questions` add/answer (A4), `spec approve` (A5),
`dispatch --spec` (A6), per-prose-card verdicts, the serve API (B1).

## Migration / compat

Purely additive — no model change, no change to existing commands.

## Testing

- **review:** emits all acceptance_criteria (id/text/verdict) + open_questions +
  context (problem/user_story/approach parsed from body; non_goals/risks/repos
  from frontmatter) + correct summary counts; `--json` is valid JSON; unknown id
  errors.
- **verdict:** sets a criterion's verdict (round-trips through `SpecStore`); a
  reviewed verdict shows up in a subsequent `spec review`; invalid verdict value
  errors (lists valid set); unknown criterion id errors (lists valid ids);
  prose-unit id (`problem`) errors; status is unchanged.

## Follow-ons

- A5 (`approve`) consumes these verdicts (e.g. refuse approve while any criterion
  is `flagged` or `unreviewed`).
- Per-prose-card verdicts (Problem/Non-goals/Scope-risk) if C4 wants them — a
  `review_verdicts` map on the Spec, deferred under YAGNI.

from __future__ import annotations

from datetime import datetime

from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec, SpecDraft
from mship.core.spec_body import render_body
from mship.util.slug import slugify


SPEC_BODY_TEMPLATE = """\
## Problem

_What problem does this solve? Why now?_

## User story

_As a <user>, I want <capability>, so that <benefit>._

## Approach

_How will it work? Key decisions._
"""


def new_spec(
    title: str,
    *,
    now: datetime,
    spec_id: str | None = None,
    affected_repos: list[str] | None = None,
    task_slug: str | None = None,
) -> Spec:
    """Construct a fresh spec in `drafting` with the canonical empty body.

    Pure: builds and returns the `Spec` but does NOT persist it — callers own
    save + collision handling. `spec_id` defaults to a slug of the title;
    raises `ValueError` if the title yields an empty slug and no id is given.
    """
    if spec_id is None:
        spec_id = slugify(title)
    if not spec_id:
        raise ValueError(
            f"could not derive a spec id from title {title!r}; pass spec_id explicitly"
        )
    return Spec(
        id=spec_id,
        title=title,
        status="drafting",
        created_at=now,
        updated_at=now,
        affected_repos=list(affected_repos or []),
        task_slug=task_slug,
        body=SPEC_BODY_TEMPLATE,
    )


def build_draft_prompt(spec_id: str, intent_text: str) -> str:
    """Agent-agnostic prompt: turn intent into a SpecDraft JSON. No model call."""
    return f"""\
# Draft spec `{spec_id}`

Turn the intent below into a structured spec. Output **only** a single JSON
object matching this shape — no prose, no markdown fence:

{{
  "problem": "<why this matters>",
  "user_story": "As a <user>, I want <capability>, so that <benefit>.",
  "approach": "<how it works; key decisions>",
  "non_goals": ["<explicitly out of scope>"],
  "risks": ["<known risk>"],
  "affected_repos": ["<repo>"],
  "acceptance_criteria": ["<testable, user-visible outcome>"],
  "open_questions": ["<must be resolved before approval>"],
  "additional_sections": [{{"heading": "<optional extra section, e.g. Architecture | Testing | Security>", "body": "<prose; include only for design-heavy specs>"}}]
}}

## Intent

{intent_text}

## Apply your result

Save the JSON to a file and run:

    mship spec apply {spec_id} --from-json <file>

or pipe it directly:

    cat draft.json | mship spec apply {spec_id} --from-json -
"""


def apply_draft(spec: Spec, draft: SpecDraft) -> Spec:
    """Merge a SpecDraft into `spec` in place: render the canonical body, set the
    structured fields, and assign deterministic `ac`/`q` ids. Does NOT change
    status/updated_at — the caller owns the lifecycle transition + persistence."""
    spec.body = render_body(
        draft.problem, draft.user_story, draft.approach,
        additional_sections=[(s.heading, s.body) for s in draft.additional_sections],
    )
    spec.non_goals = list(draft.non_goals)
    spec.risks = list(draft.risks)
    spec.affected_repos = list(draft.affected_repos)
    # Preserve verdict + evidence for a criterion whose TEXT is unchanged, matched
    # by text (not positional id) so inserting, removing, or reordering an earlier
    # criterion doesn't reset verification on the ones that didn't change. Ids stay
    # positional (ac{i+1}); each prior criterion is consumed at most once so that
    # duplicate-text criteria can't both claim the same prior's evidence.
    prior_by_text: dict[str, list[AcceptanceCriterion]] = {}
    for c in spec.acceptance_criteria:
        prior_by_text.setdefault(c.text, []).append(c)
    new_acs: list[AcceptanceCriterion] = []
    for i, t in enumerate(draft.acceptance_criteria):
        ac_id = f"ac{i + 1}"
        bucket = prior_by_text.get(t)
        if bucket:
            # text unchanged → carry forward verdict + evidence (consume the match).
            prior = bucket.pop(0)
            new_acs.append(AcceptanceCriterion(
                id=ac_id, text=t, verdict=prior.verdict,
                evidence=list(prior.evidence),
            ))
        else:
            # new or materially-changed criterion → start fresh.
            new_acs.append(AcceptanceCriterion(id=ac_id, text=t))
    spec.acceptance_criteria = new_acs
    spec.open_questions = [
        OpenQuestion(id=f"q{i + 1}", text=t)
        for i, t in enumerate(draft.open_questions)
    ]
    return spec

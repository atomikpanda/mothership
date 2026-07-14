from __future__ import annotations

from datetime import datetime

from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec, SpecDraft
from mship.core.spec_body import parse_body_sections, render_body
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
    """Construct a fresh spec in `draft` with the canonical empty body.

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
        status="draft",
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
    # Capture the OLD prose text for each comparable section id BEFORE any field is
    # overwritten below — the canonical problem/user_story/approach live in `spec.body`
    # (parse it now, since `render_body` is about to clobber it), while non_goals/risks
    # are list fields. Used just below to decide which prose verdicts survive.
    old_sections = parse_body_sections(spec.body)
    old_prose_text: dict[str, object] = {
        "problem": old_sections.get("Problem", ""),
        "user_story": old_sections.get("User story", ""),
        "approach": old_sections.get("Approach", ""),
        "non_goals": list(spec.non_goals),
        "risks": list(spec.risks),
    }

    spec.body = render_body(
        draft.problem, draft.user_story, draft.approach,
        additional_sections=[(s.heading, s.body) for s in draft.additional_sections],
    )
    spec.non_goals = list(draft.non_goals)
    spec.risks = list(draft.risks)
    spec.affected_repos = list(draft.affected_repos)
    # Prose-section verdicts across a re-draft (MOS-172, Greptile #344): a verdict is
    # preserved only when the section's TEXT is UNCHANGED, and dropped (re-reviewed)
    # when it changed — mirroring the acceptance-criteria matcher below (a rewritten
    # section must not keep a stale approval, nor stay blocked after it's fixed). We
    # compare the new text (parsed-body prose stripped exactly as render_body writes
    # it; list fields compared as lists) against the OLD text captured above. Section
    # ids with no draft-derived text (e.g. `scope_risk`, or any unknown/legacy key)
    # have nothing to compare, so they carry over unchanged.
    new_prose_text: dict[str, object] = {
        "problem": draft.problem.strip(),
        "user_story": draft.user_story.strip(),
        "approach": draft.approach.strip(),
        "non_goals": list(draft.non_goals),
        "risks": list(draft.risks),
    }
    preserved_prose = {}
    for sid, pv in spec.prose_verdicts.items():
        if sid in old_prose_text:
            if old_prose_text[sid] == new_prose_text[sid]:
                preserved_prose[sid] = pv  # unchanged text → keep verdict
            # else: text changed → drop so it is re-reviewed
        else:
            preserved_prose[sid] = pv  # no comparable text → carry over unchanged
    spec.prose_verdicts = preserved_prose
    # Preserve verdict + evidence for unchanged criteria across a re-apply. Criteria
    # have no stable id (ids are positional, ac{i+1}), so match each new criterion to
    # a prior one in two passes, consuming each prior at most once:
    #   Pass 1 — exact (same id AND same text): the strongest "same criterion" signal.
    #     Pins an unchanged criterion to its position even when an unrelated edit makes
    #     a *sibling*'s text collide with it (so the unchanged one keeps its evidence
    #     and the edited one can't steal it).
    #   Pass 2 — by text among the still-unmatched priors: recovers criteria whose
    #     position shifted (insert / remove / reorder) but whose text didn't change.
    # Whatever stays unmatched is new or materially-changed and starts fresh. Because
    # priors are consumed, evidence is never duplicated onto two criteria.
    prior_acs = list(spec.acceptance_criteria)
    consumed = [False] * len(prior_acs)
    matched: list[AcceptanceCriterion | None] = [None] * len(draft.acceptance_criteria)

    for i, t in enumerate(draft.acceptance_criteria):
        ac_id = f"ac{i + 1}"
        for j, p in enumerate(prior_acs):
            if not consumed[j] and p.id == ac_id and p.text == t:
                matched[i], consumed[j] = p, True
                break

    for i, t in enumerate(draft.acceptance_criteria):
        if matched[i] is not None:
            continue
        for j, p in enumerate(prior_acs):
            if not consumed[j] and p.text == t:
                matched[i], consumed[j] = p, True
                break

    new_acs: list[AcceptanceCriterion] = []
    for i, t in enumerate(draft.acceptance_criteria):
        ac_id = f"ac{i + 1}"
        prior = matched[i]
        if prior is not None:
            new_acs.append(AcceptanceCriterion(
                id=ac_id, text=t, verdict=prior.verdict,
                evidence=list(prior.evidence),
            ))
        else:
            new_acs.append(AcceptanceCriterion(id=ac_id, text=t))
    spec.acceptance_criteria = new_acs
    spec.open_questions = [
        OpenQuestion(id=f"q{i + 1}", text=t)
        for i, t in enumerate(draft.open_questions)
    ]
    return spec

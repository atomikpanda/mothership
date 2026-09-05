from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mship.core.spec import (
    AcceptanceCriterion, BodySection, OpenQuestion, Spec, SpecDraft, validate_transition,
)
from mship.core.spec_body import parse_body_sections, render_body
from mship.core.spec_store import SpecStore
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


@dataclass(frozen=True)
class MissingSpec:
    """The requested spec id had no current artifact to apply."""

    spec_id: str


class ReviewDiscardRequired(Exception):
    """Applying would remove protected review metadata without authorization."""

    def __init__(self, discarded_review_count: int) -> None:
        self.discarded_review_count = discarded_review_count
        unit_noun = "unit" if discarded_review_count == 1 else "units"
        super().__init__(
            f"Cannot apply draft: review state for {discarded_review_count} review "
            f"{unit_noun} would be discarded. Re-run with --discard-review to continue."
        )


@dataclass(frozen=True)
class ApplyDraftResult:
    spec: Spec
    path: Path
    discarded_review_count: int


def _protected_review_unit_count(spec: Spec) -> int:
    return (
        sum(
            criterion.verdict != "unreviewed"
            or bool(criterion.comment)
            or bool(criterion.evidence)
            for criterion in spec.acceptance_criteria
        )
        + sum(
            verdict.verdict != "unreviewed" or bool(verdict.comment)
            for verdict in spec.prose_verdicts.values()
        )
        + sum(question.answer is not None for question in spec.open_questions)
    )


def apply_draft_transaction(
    store: SpecStore,
    spec_id: str,
    draft: SpecDraft,
    *,
    bypass_status_gate: bool = False,
    discard_review: bool = False,
    now: datetime | None = None,
) -> ApplyDraftResult | MissingSpec:
    """Atomically apply a draft and persist its lifecycle transition."""
    with store.locked(spec_id) as artifact:
        if artifact is None:
            return MissingSpec(spec_id=spec_id)

        applied = artifact.spec.model_copy(deep=True)
        apply_draft(applied, draft)
        discarded_review_count = (
            _protected_review_unit_count(artifact.spec)
            - _protected_review_unit_count(applied)
        )
        if discarded_review_count and not discard_review:
            raise ReviewDiscardRequired(discarded_review_count)
        if not bypass_status_gate:
            validate_transition(applied.status, "needs_review")

        applied.status = "needs_review"
        applied.clarification_reason = None
        applied.updated_at = now or datetime.now(timezone.utc)
        path = store.save_while_locked(applied, artifact)
        return ApplyDraftResult(
            spec=applied,
            path=path,
            discarded_review_count=discarded_review_count,
        )


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
                evidence=list(prior.evidence), comment=prior.comment,
            ))
        else:
            new_acs.append(AcceptanceCriterion(id=ac_id, text=t))
    spec.acceptance_criteria = new_acs

    prior_questions = list(spec.open_questions)
    consumed_questions = [False] * len(prior_questions)
    matched_questions: list[OpenQuestion | None] = [None] * len(draft.open_questions)
    for i, text in enumerate(draft.open_questions):
        question_id = f"q{i + 1}"
        for j, prior in enumerate(prior_questions):
            if not consumed_questions[j] and prior.id == question_id and prior.text == text:
                matched_questions[i], consumed_questions[j] = prior, True
                break
    for i, text in enumerate(draft.open_questions):
        if matched_questions[i] is not None:
            continue
        for j, prior in enumerate(prior_questions):
            if not consumed_questions[j] and prior.text == text:
                matched_questions[i], consumed_questions[j] = prior, True
                break
    spec.open_questions = [
        OpenQuestion(
            id=f"q{i + 1}",
            text=text,
            answer=prior.answer if prior is not None else None,
        )
        for i, (text, prior) in enumerate(zip(draft.open_questions, matched_questions))
    ]
    return spec


_PROSE_SECTIONS = {
    "problem": "problem",
    "user story": "user_story",
    "approach": "approach",
}
_LIST_SECTIONS = {
    "acceptance criteria": "acceptance_criteria",
    "open questions": "open_questions",
    "non-goals": "non_goals",
    "non goals": "non_goals",
    "risks": "risks",
    "affected repos": "affected_repos",
    "affected repositories": "affected_repos",
}
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.*)$")
_CHECKBOX_RE = re.compile(r"^\[[ xX]\]\s+(.*)$")
_ID_BACKTICK_RE = re.compile(r"^`[A-Za-z]+\d+`\s+(.*)$")
_ID_BRACKET_RE = re.compile(r"^\[[A-Za-z]+\d+\]\s+(.*)$")


def _parse_list_items(heading: str, raw: str) -> list[str]:
    """Extract bullet-item text from a list section, stripping an optional
    `[ ]`/`[x]` checkbox and an optional `` `acN` `` / `[acN]` id token. The
    `\\d+` requirement on ids means real prose like `` `code` does X `` is never
    mistaken for an id."""
    items: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        m = _BULLET_RE.match(line)
        if m is None:
            raise ValueError(
                f"cannot parse spec markdown: '{heading}' section has a "
                f"non-list line: {line.strip()!r}"
            )
        text = m.group(1).strip()
        cb = _CHECKBOX_RE.match(text)
        if cb is not None:
            text = cb.group(1).strip()
        idm = _ID_BACKTICK_RE.match(text) or _ID_BRACKET_RE.match(text)
        if idm is not None:
            text = idm.group(1).strip()
        if text:
            items.append(text)
    return items


def parse_spec_markdown(text: str) -> SpecDraft:
    """Parse a rendered spec markdown document back into a SpecDraft.

    Inverse of the body/section rendering used across mship. Reuses
    `parse_body_sections` to split by `## ` headings, maps known headings to
    SpecDraft fields, parses list sections into text-only items, and preserves
    any other `## <Heading>` section as an additional_sections entry (matching
    how `render_body` appends extras after Approach).
    """
    sections = parse_body_sections(text)
    present = {heading.strip().lower() for heading in sections}
    missing = [name for name in ("Problem", "User story", "Approach") if name.lower() not in present]
    if missing:
        raise ValueError(
            "cannot parse spec markdown: missing required section(s): " + ", ".join(missing)
        )
    fields: dict[str, object] = {
        "problem": "", "user_story": "", "approach": "",
        "non_goals": [], "risks": [], "affected_repos": [],
        "acceptance_criteria": [], "open_questions": [],
    }
    additional: list[BodySection] = []
    for heading, body in sections.items():
        key = heading.strip().lower()
        if key in _PROSE_SECTIONS:
            fields[_PROSE_SECTIONS[key]] = body.strip()
        elif key in _LIST_SECTIONS:
            fields[_LIST_SECTIONS[key]] = _parse_list_items(heading.strip(), body)
        else:
            additional.append(BodySection(heading=heading.strip(), body=body.strip()))
    return SpecDraft(additional_sections=additional, **fields)

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, model_validator


# MOS-240: collapsed status vocabulary (was 8: captured/drafting/needs_review/
# needs_clarification/approved/dispatched/implemented/archived). `captured` +
# `drafting` merged into `draft`; `needs_clarification` dropped — "needs
# clarification" is now expressed by a non-null `clarification_reason` on any
# status.
SpecStatus = Literal[
    "draft", "needs_review", "approved", "dispatched", "implemented", "archived",
]

# Legacy statuses are mapped forward at EVERY Spec construction path via the model
# validator below — not just `parse_spec` — so `Spec.model_validate_json(...)`,
# direct construction, or any client loading old serialized Spec data never errors
# on the removed literals. `captured`/`drafting`/`needs_clarification` → `draft`;
# for `needs_clarification` the "sent back for changes" signal is preserved as a
# `clarification_reason` (matching how `mship spec request-changes` writes it).
LEGACY_STATUS_MAP: dict[str, str] = {
    "captured": "draft",
    "drafting": "draft",
    "needs_clarification": "draft",
}
_MIGRATED_CLARIFICATION_REASON = "needs clarification (migrated from needs_clarification status)"


class AcceptanceEvidence(BaseModel):
    kind: Literal["test", "commit", "artifact"]
    ref: str
    note: str | None = None


class AcceptanceCriterion(BaseModel):
    id: str
    text: str
    verdict: Literal["unreviewed", "approved", "flagged"] = "unreviewed"
    evidence: list[AcceptanceEvidence] = []


class OpenQuestion(BaseModel):
    id: str
    text: str
    answer: str | None = None


class Spec(BaseModel):
    id: str
    title: str
    status: SpecStatus
    created_at: datetime
    updated_at: datetime
    affected_repos: list[str] = []
    acceptance_criteria: list[AcceptanceCriterion] = []
    open_questions: list[OpenQuestion] = []
    non_goals: list[str] = []
    risks: list[str] = []
    task_slug: str | None = None
    body: str = ""
    work_item_id: str | None = None
    clarification_reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_status(cls, data):
        """Map pre-MOS-240 statuses forward at EVERY construction path (MOS-240).

        Runs for `model_validate`/`model_validate_json`/direct-dict construction —
        so old serialized Spec data loads cleanly regardless of whether it came
        through `parse_spec`, a client, or a script — before the `SpecStatus`
        literal is enforced.
        """
        if not isinstance(data, dict):
            return data
        old = data.get("status")
        new = LEGACY_STATUS_MAP.get(old)
        if new is not None:
            data["status"] = new
            if old == "needs_clarification" and not data.get("clarification_reason"):
                data["clarification_reason"] = _MIGRATED_CLARIFICATION_REASON
        return data

    @property
    def dispatch_ready(self) -> bool:
        # acceptance_criteria verdicts are gated at approval time (status == "approved");
        # they are intentionally not re-checked here. Only open questions block dispatch.
        return self.status == "approved" and all(
            q.answer is not None for q in self.open_questions
        )


TERMINAL_STATUSES: set[str] = {"archived"}

# MOS-240: with `needs_clarification` gone, "send back for changes" (aka
# request-changes) is modelled as a move to the editable `draft` status carrying a
# non-null `clarification_reason`, from either `needs_review` or `approved`
# (re-open). This preserves the old flow's reachability:
#   old drafting→needs_review           => draft→needs_review
#   old needs_review→needs_clarification => needs_review→draft (+ reason)
#   old approved→needs_clarification     => approved→draft (+ reason)
#   old needs_clarification→needs_review => draft→needs_review (re-apply)
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"needs_review"},
    "needs_review": {"draft", "approved"},
    "approved": {"dispatched", "draft"},
    "dispatched": {"implemented"},
    "implemented": {"archived"},
    "archived": set(),
}


class InvalidTransition(Exception):
    pass


def can_transition(current: str, target: str) -> bool:
    """Whether `current` may transition to `target`.

    Takes raw strings (not the `SpecStatus` Literal) for caller convenience;
    unknown `current` values simply have no outgoing edges. The map is
    authoritative; the abandon rule is an additive fallback that lets any
    non-terminal status jump straight to `archived`.
    """
    if current == target:
        return False
    if target in ALLOWED_TRANSITIONS.get(current, set()):
        return True
    # Abandon: any non-terminal status may jump to archived.
    if target == "archived" and current not in TERMINAL_STATUSES:
        return True
    return False


def validate_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(f"illegal spec transition: {current} -> {target}")


class BodySection(BaseModel):
    """An extra spec body section beyond the canonical three (e.g. Architecture, Testing)."""
    heading: str
    body: str


class SpecDraft(BaseModel):
    """The draftable subset a model produces; ingested by `mship spec apply`.

    Criteria/questions are plain text — mship assigns their ids on apply.
    """
    problem: str
    user_story: str
    approach: str
    non_goals: list[str] = []
    risks: list[str] = []
    affected_repos: list[str] = []
    acceptance_criteria: list[str] = []
    open_questions: list[str] = []
    additional_sections: list[BodySection] = []

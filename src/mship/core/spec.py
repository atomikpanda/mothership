from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


SpecStatus = Literal[
    "captured", "drafting", "needs_review", "needs_clarification",
    "approved", "dispatched", "implemented", "archived",
]


class AcceptanceCriterion(BaseModel):
    id: str
    text: str
    verdict: Literal["unreviewed", "approved", "flagged"] = "unreviewed"


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

    @property
    def dispatch_ready(self) -> bool:
        # acceptance_criteria verdicts are gated at approval time (status == "approved");
        # they are intentionally not re-checked here. Only open questions block dispatch.
        return self.status == "approved" and all(
            q.answer is not None for q in self.open_questions
        )


TERMINAL_STATUSES: set[str] = {"archived"}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "captured": {"drafting"},
    "drafting": {"needs_review"},
    "needs_review": {"needs_clarification", "approved"},
    "needs_clarification": {"needs_review", "drafting"},
    "approved": {"dispatched", "needs_clarification"},
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

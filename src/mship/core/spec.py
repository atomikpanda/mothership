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

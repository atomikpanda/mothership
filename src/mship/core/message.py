from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, computed_field, model_validator


class DecisionPayload(BaseModel):
    options: list[str]
    recommended: int | None = None
    allow_free_text: bool = True


class Message(BaseModel):
    id: str
    thread_id: str
    role: Literal["human", "agent"]
    text: str
    created_at: datetime
    # "needs_you" marks an agent message that needs the operator to act
    # (surfaces as a Home action card in Ground Control).
    # "decision" marks an agent message presenting a typed decision (see
    # DecisionPayload / Thread.needs_decision). Default "note".
    kind: Literal["note", "needs_you", "decision"] = "note"
    decision: DecisionPayload | None = None

    @model_validator(mode="after")
    def decision_kind_requires_payload(self) -> "Message":
        if self.kind == "decision" and self.decision is None:
            raise ValueError("kind='decision' requires a decision payload")
        return self


class Thread(BaseModel):
    id: str
    subject: str
    created_at: datetime
    updated_at: datetime
    task_slug: str | None = None
    spec_id: str | None = None
    # Operator read cursor: the operator has seen messages up to this time.
    seen_at: datetime | None = None
    messages: list[Message] = []

    @computed_field  # serialized into model_dump()/JSON (a plain @property is not)
    @property
    def awaiting_reply(self) -> bool:
        """A thread needs an agent iff its latest message is from a human."""
        return bool(self.messages) and self.messages[-1].role == "human"

    @computed_field
    @property
    def needs_you(self) -> bool:
        """True iff an agent message marked needs_you is unanswered — i.e. newer
        than the operator's last human message. Survives a follow-up plain note."""
        last_human = -1
        for i, m in enumerate(self.messages):
            if m.role == "human":
                last_human = i
        return any(
            m.role == "agent" and m.kind == "needs_you"
            for m in self.messages[last_human + 1:]
        )

    @computed_field
    @property
    def needs_decision(self) -> bool:
        """True iff an unanswered agent message with kind=decision exists after the
        operator's last human message (mirrors needs_you)."""
        last_human = -1
        for i, m in enumerate(self.messages):
            if m.role == "human":
                last_human = i
        return any(
            m.role == "agent" and m.kind == "decision"
            for m in self.messages[last_human + 1:]
        )

    @computed_field
    @property
    def unseen(self) -> bool:
        """True iff the latest agent message is newer than the operator's seen cursor."""
        latest_agent = None
        for m in self.messages:
            if m.role == "agent":
                latest_agent = m
        if latest_agent is None:
            return False
        return self.seen_at is None or latest_agent.created_at > self.seen_at

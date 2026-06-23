from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, computed_field


class Message(BaseModel):
    id: str
    thread_id: str
    role: Literal["human", "agent"]
    text: str
    created_at: datetime


class Thread(BaseModel):
    id: str
    subject: str
    created_at: datetime
    updated_at: datetime
    task_slug: str | None = None
    messages: list[Message] = []

    @computed_field  # serialized into model_dump()/JSON (a plain @property is not)
    @property
    def awaiting_reply(self) -> bool:
        """A thread needs an agent iff its latest message is from a human."""
        return bool(self.messages) and self.messages[-1].role == "human"

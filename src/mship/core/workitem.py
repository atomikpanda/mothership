# src/mship/core/workitem.py
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Kind = Literal["feature", "bug", "chore", "question"]
Phase = Literal["inbox", "shaping", "ready", "in_flight", "review", "done"]

PHASE_ORDER: tuple[Phase, ...] = ("inbox", "shaping", "ready", "in_flight", "review", "done")


class ExternalLink(BaseModel):
    provider: Literal["github", "linear", "notion", "jira", "url"]
    url: str
    title: str = ""


class WorkItem(BaseModel):
    id: str
    title: str
    workspace: str
    kind: Kind
    created_at: datetime
    updated_at: datetime
    spec_id: str | None = None
    task_slugs: list[str] = []
    thread_ids: list[str] = []
    external_links: list[ExternalLink] = []
    # Manual nudge: when set, overrides the phase derived from child state.
    phase_override: Phase | None = None

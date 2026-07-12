from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Kind = Literal["feature", "bug", "chore", "question"]
Phase = Literal["inbox", "shaping", "ready", "in_flight", "review", "done"]
Provider = Literal["github", "linear", "notion", "jira", "url"]

PHASE_ORDER: tuple[Phase, ...] = ("inbox", "shaping", "ready", "in_flight", "review", "done")


class ExternalLink(BaseModel):
    provider: Provider
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
    plan_path: str | None = None
    task_slugs: list[str] = []
    thread_ids: list[str] = []
    external_links: list[ExternalLink] = []
    # Manual nudge: when set, overrides the phase derived from child state.
    phase_override: Phase | None = None
    # Opt-in: this item is eligible for unattended (cloud-runner) execution. #unattended-runner
    unattended: bool = False
    # Soft, reversible archive: excluded from list() by default. Missing on legacy
    # (pre-archive) JSON files, which pydantic defaults to False on load.
    archived: bool = False

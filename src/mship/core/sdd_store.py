"""Dispatch-record store under `<state_dir>/sdd/` (spec mship-dispatch-v2).

A record is metadata + a POINTER to canonical content — plan path + anchor id
(or an ad-hoc instruction for non-plan dispatches) — never a copy of plan
prose. Prompts are derived at emit time from the plan and spec stores, so an
edited plan is reflected on the next emit and no second copy can drift.
Layout: `<state_dir>/sdd/<work-item-id | no-item>/<task-slug>/record.json`,
with review-package artifacts (Task 6) beside it. Everything here is
git-ignored with the rest of `.mothership/` and removed by `mship close`.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

_NO_ITEM = "no-item"


class DispatchRecord(BaseModel):
    task_slug: str
    work_item_id: str | None
    mode: str
    model: str
    repo: str
    worktree: str
    base_branch: str
    base_sha: str | None
    head_sha: str | None
    # Exactly one content pointer: (plan_path + plan_task_id) or instruction.
    plan_path: str | None
    plan_task_id: str | None
    acs: list[str] = []
    instruction: str | None
    created_at: datetime


class SddStore:
    def __init__(self, state_dir: Path):
        self.root = state_dir / "sdd"

    def _dir(self, work_item_id: str | None, task_slug: str) -> Path:
        return self.root / (work_item_id or _NO_ITEM) / task_slug

    def write(self, rec: DispatchRecord) -> Path:
        d = self._dir(rec.work_item_id, rec.task_slug)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "record.json"
        path.write_text(rec.model_dump_json(indent=2) + "\n")
        return path

    def read(self, *, work_item_id: str | None, task_slug: str) -> DispatchRecord:
        path = self._dir(work_item_id, task_slug) / "record.json"
        return DispatchRecord.model_validate(json.loads(path.read_text()))

    def find_for_slug(self, task_slug: str) -> DispatchRecord | None:
        """Locate a record by slug alone (the subagent's emit path — it knows
        its task from cwd, not its WorkItem)."""
        for p in sorted(self.root.glob(f"*/{task_slug}/record.json")):
            return DispatchRecord.model_validate(json.loads(p.read_text()))
        return None

    def remove_task(self, task_slug: str) -> None:
        """Remove every record dir for this slug (close-time teardown)."""
        if not self.root.is_dir():
            return
        for d in self.root.glob(f"*/{task_slug}"):
            shutil.rmtree(d, ignore_errors=True)
        for item_dir in self.root.iterdir():
            if item_dir.is_dir() and not any(item_dir.iterdir()):
                item_dir.rmdir()

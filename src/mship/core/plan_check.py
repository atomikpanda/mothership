from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

__all__ = [
    "AxisVerdict",
    "Flag",
    "PlanCheckResult",
    "PlanCheckStore",
    "flags_from_verdicts",
    "plan_hash",
]


def plan_hash(plan_text: str) -> str:
    """sha256 hex of `plan_text` with trailing whitespace stripped per line and a
    single trailing newline, so cosmetic whitespace edits don't churn the hash."""
    normalized = "\n".join(line.rstrip() for line in plan_text.splitlines()) + "\n"
    return hashlib.sha256(normalized.encode()).hexdigest()


class AxisVerdict(BaseModel):
    axis: str
    verdict: Literal["covered", "not-covered", "n-a"]
    reason: str


class Flag(BaseModel):
    axis: str
    source: Literal["checker", "cross-check"]
    reason: str
    approved: bool = False
    approved_by: str | None = None
    approved_reason: str | None = None


class PlanCheckResult(BaseModel):
    task_slug: str
    plan_hash: str
    verdicts: list[AxisVerdict]
    flags: list[Flag]


def flags_from_verdicts(verdicts: list[AxisVerdict]) -> list[Flag]:
    """One `source="checker"` flag per `not-covered` verdict; `covered`/`n-a` produce none."""
    return [
        Flag(axis=v.axis, source="checker", reason=v.reason)
        for v in verdicts
        if v.verdict == "not-covered"
    ]


class PlanCheckStore:
    """Filesystem registry for plan-check results: one JSON file per task, under
    `<state_dir>/plan-checks/`."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir) / "plan-checks"

    def path(self, task_slug: str) -> Path:
        return self._dir / f"{task_slug}.json"

    def save(self, result: PlanCheckResult) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self.path(result.task_slug)
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".json.tmp")
        try:
            with open(fd, "w") as f:
                f.write(result.model_dump_json(indent=2))
            Path(tmp).replace(path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def get(self, task_slug: str) -> PlanCheckResult | None:
        path = self.path(task_slug)
        if not path.is_file():
            return None
        return PlanCheckResult.model_validate_json(path.read_text())

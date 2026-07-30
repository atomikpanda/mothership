from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from mship.core.plan import _normalize_axis

if TYPE_CHECKING:
    from mship.core.assumptions import AssumptionRow

__all__ = [
    "AxisVerdict",
    "Flag",
    "PlanCheckResult",
    "PlanCheckStore",
    "cross_check",
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


def _triggers_match(triggers: str, plan_text: str, task_text: str, affected_repos: list[str]) -> bool:
    """A row's `triggers` cell is comma-separated tokens. A `foo/*` token matches by
    prefix `foo/` against either text blob; a plain token matches as a case-insensitive
    substring of either text blob, or equals an `affected_repos` entry (case-insensitive)."""
    haystacks = [plan_text.lower(), task_text.lower()]
    repos_lower = [r.lower() for r in affected_repos]
    for raw_token in triggers.split(","):
        token = raw_token.strip().lower()
        if not token:
            continue
        if token.endswith("/*"):
            prefix = token[:-1]  # keep trailing "/"
            if any(prefix in haystack for haystack in haystacks):
                return True
        else:
            if any(token in haystack for haystack in haystacks):
                return True
            if token in repos_lower:
                return True
    return False


def cross_check(
    verdicts: list[AxisVerdict],
    rows: list["AssumptionRow"],
    *,
    plan_text: str,
    task_text: str,
    affected_repos: list[str],
) -> list[Flag]:
    """Deterministic (no LLM) trigger cross-check: for each assumption row whose
    `triggers` match the plan/task context, the plan must have addressed that axis
    as `covered` or `not-covered`. A matching `n-a` verdict, or no verdict at all,
    is a contradiction between the declared position and the plan's own triggers —
    surfaced as a `source="cross-check"` flag. Only adds flags; never removes the
    checker's own `not-covered` flags."""
    verdict_by_axis = {_normalize_axis(v.axis): v for v in verdicts}
    flags: list[Flag] = []
    for row in rows:
        if not _triggers_match(row.triggers, plan_text, task_text, affected_repos):
            continue
        verdict = verdict_by_axis.get(_normalize_axis(row.axis))
        if verdict is None:
            flags.append(
                Flag(
                    axis=row.axis,
                    source="cross-check",
                    reason=(
                        f"triggers for '{row.axis}' match this plan/task, but the plan "
                        "has no verdict for this axis"
                    ),
                )
            )
        elif verdict.verdict == "n-a":
            flags.append(
                Flag(
                    axis=row.axis,
                    source="cross-check",
                    reason=(
                        f"triggers for '{row.axis}' match this plan/task, but the plan "
                        "declared it n/a"
                    ),
                )
            )
    return flags


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

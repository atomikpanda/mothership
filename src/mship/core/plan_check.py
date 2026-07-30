from __future__ import annotations

import fcntl
import hashlib
import re
import tempfile
from contextlib import contextmanager
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


def flags_from_verdicts(verdicts: list[AxisVerdict], rows: list["AssumptionRow"]) -> list[Flag]:
    """One `source="checker"` pending flag per CANONICAL row the checker did not
    dispose of as covered/n-a — driven by `rows`, NOT by the verdict array, so the
    gate enforces completeness:

    - a `not-covered` verdict → pending flag (reason = the checker's line);
    - NO verdict for the row (the checker's JSON omitted it) → pending flag. An
      omitted row must never silently pass the plan→dev gate — that is exactly the
      "disposition every assumption" invariant this system exists to hold (#444);
    - `covered`/`n-a` → no checker flag (a triggered `n-a` is still caught by
      `cross_check`).

    Verdicts whose axis is NOT a current row are IGNORED — an invented or
    misspelled axis must not manufacture a phantom flag the operator then has to
    approve for a row that does not exist."""
    verdict_by_axis = {_normalize_axis(v.axis): v for v in verdicts}
    flags: list[Flag] = []
    for row in rows:
        v = verdict_by_axis.get(_normalize_axis(row.axis))
        if v is None:
            flags.append(
                Flag(axis=row.axis, source="checker",
                     reason="checker returned no verdict for this row")
            )
        elif v.verdict == "not-covered":
            flags.append(Flag(axis=row.axis, source="checker", reason=v.reason))
    return flags


def _triggers_match(triggers: str, plan_text: str, task_text: str, affected_repos: list[str]) -> bool:
    """A row's `triggers` cell is comma-separated tokens. A `foo/*` token matches a
    `foo/` path SEGMENT (segment boundary, not mid-word — so `run/*` does NOT match
    `prerun/config`); a plain token matches on a WORD boundary (so `run` does NOT
    match `brunch`, and `UI` does NOT match `build`) or equals an `affected_repos`
    entry. Word/segment boundaries keep the cross-check precise — false flags spend
    the scarce resource, operator attention (#444 backtest)."""
    haystacks = [plan_text.lower(), task_text.lower()]
    repos_lower = [r.lower() for r in affected_repos]
    for raw_token in triggers.split(","):
        token = raw_token.strip().lower()
        if not token:
            continue
        if token.endswith("/*"):
            # `foo/` must start a path segment: at string start or after a non-word,
            # non-slash char (so `prerun/` does not satisfy `run/*`).
            pat = r"(?:^|[^\w/])" + re.escape(token[:-1])
            if any(re.search(pat, h) for h in haystacks):
                return True
        else:
            pat = r"\b" + re.escape(token) + r"\b"
            if any(re.search(pat, h) for h in haystacks):
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
    as `covered` or `not-covered`. A matching `n-a` verdict is a contradiction
    between the declared position and the plan's own triggers — surfaced as a
    `source="cross-check"` flag. A row with no verdict at all is NOT handled here:
    `flags_from_verdicts` already raises a completeness flag for every
    un-dispositioned row (triggered or not), so flagging it again here would force
    the operator to approve the same row twice. Only adds flags; never removes the
    checker's own `not-covered` flags."""
    verdict_by_axis = {_normalize_axis(v.axis): v for v in verdicts}
    flags: list[Flag] = []
    for row in rows:
        if not _triggers_match(row.triggers, plan_text, task_text, affected_repos):
            continue
        verdict = verdict_by_axis.get(_normalize_axis(row.axis))
        if verdict is not None and verdict.verdict == "n-a":
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
        # `slug` can reach here from an HTTP path param (serve
        # /plan-assumptions/{slug}), so treat it as untrusted at this boundary.
        # Resolve the candidate and require it to sit DIRECTLY inside plan-checks/
        # — a positive containment proof (the sanitizer pattern that clears
        # CodeQL py/path-injection, mirroring resolve_plan_path's is_relative_to
        # guard), which also subsumes any separator / `..` / absolute-path slug.
        # Callers still validate the slug against state.tasks upstream; this is
        # defense in depth, not the only guard.
        if not task_slug:
            raise ValueError("empty task slug")
        base = self._dir.resolve()
        candidate = (base / f"{task_slug}.json").resolve()
        if candidate.parent != base:
            raise ValueError(f"unsafe task slug: {task_slug!r}")
        return candidate

    def _lock_path(self, task_slug: str) -> Path:
        """Per-task lock file (`<slug>.json.lock`). Reuses `path`'s slug
        validation; not matched by any `*.json` read glob."""
        p = self.path(task_slug)
        return p.with_name(p.name + ".lock")

    @contextmanager
    def transaction(self, task_slug: str):
        """Exclusive per-task advisory lock spanning a read-modify-write (mirrors
        WorkItemStore._locked). `result` and `approve` both get→mutate→save the
        same task record; without a lock spanning the whole transaction, an
        approval overlapping a checker refresh (or two approvals) can silently
        clobber each other's write. Per-task granularity lets different tasks
        proceed in parallel."""
        self._dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_path(task_slug)
        lock_path.touch(exist_ok=True)
        with open(lock_path, "r+") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

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

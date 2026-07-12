"""Universal + kind-gated enforcement: every task must be linked to a WorkItem,
and a feature WorkItem must have an approved-or-beyond spec before dev/finish.
See spec workitem-mandatory-kind-gated-approval."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mship.core.gate import record_bypass
from mship.core.spec_store import SpecStore
from mship.core.workitem import WorkItem
from mship.core.workitem_store import WorkItemStore

# "approved or beyond" — the single source of truth for this status set.
# Imported by workitem_migrate.wrap_existing and PhaseManager._has_approved_spec
# so all three enforcement sites agree on what counts as approved.
APPROVED_STATUSES = {"approved", "dispatched", "implemented"}


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason: str | None = None  # actionable message when not ok


def check_task_gate(task, workspace_root: Path, require_plan: bool = False) -> GateResult:
    """Universal: a task must have a WorkItem. Kind-gated: a feature WorkItem
    must have an approved spec. When `require_plan` is set — only at phase
    plan→dev and finish, never at spawn — a feature WorkItem must ALSO have a
    valid implementation plan. bug/chore/question need only the WorkItem."""
    if getattr(task, "work_item_id", None) is None:
        return GateResult(False, "no WorkItem — create one with `mship item new --kind <kind>` "
                                 "and spawn with `--work-item <id>` (or pass `--hotfix` to override)")
    items = WorkItemStore(Path(workspace_root) / ".mothership" / "workitems")
    wi = items.get(task.work_item_id)
    if wi is None:
        return GateResult(False, f"work_item_id {task.work_item_id!r} not found")
    if wi.kind == "feature" and not _feature_has_approved_spec(wi, task, workspace_root):
        return GateResult(False, "feature WorkItem requires an approved spec before dev/finish "
                                 "(approve it in Ground Control, or `--hotfix` to override)")
    if require_plan and wi.kind == "feature" and not _feature_has_plan(wi, task, workspace_root):
        dd = _docs_dir(workspace_root)
        return GateResult(False, f"feature WorkItem requires an implementation plan before dev/finish — "
                                 f"write one (writing-plans) at {dd}/plans/<date>-{task.slug}.md, or link it "
                                 f"with `mship item link-plan {task.work_item_id} <path>`. "
                                 f"Use --bypass-plan-gate / --hotfix to skip.")
    return GateResult(True)


def _feature_has_approved_spec(wi: WorkItem, task, workspace_root: Path) -> bool:
    specs = SpecStore(Path(workspace_root) / "specs")
    if wi.spec_id:
        s = specs.find_by_id(wi.spec_id)
        if s is not None and s.status in APPROVED_STATUSES:
            return True
    return any(s.task_slug == task.slug and s.status in APPROVED_STATUSES for s in specs.list())


def resolve_bound_spec(task, workspace_root: Path):
    """Return the Spec bound to `task`, or None when nothing is unambiguously bound.

    Resolution never GUESSES:
    - If the task's WorkItem has an explicit `spec_id`, that link is AUTHORITATIVE and
      TERMINAL: return the linked spec (regardless of status), or None if it's gone
      (deleted/renamed). It does NOT fall through to a slug guess — an explicit intent
      exists, so binding some other spec would be wrong.
    - Otherwise, fall back to a spec whose `task_slug` matches AND is approved, but
      bind ONLY when exactly one such spec exists. If several match, the mapping is
      ambiguous (which is "the" spec is undecidable from the data) and any pick risks
      enforcing/rendering the wrong checklist, so refuse to guess and return None.

    Full disambiguation of the fallback needs stable spec identity — tracked in
    MOS-247.

    Returns None cleanly when the stores are simply empty/absent (missing dir →
    empty list, unknown id → None). It does NOT swallow genuine read/parse errors
    (e.g. a corrupt spec file): those propagate so callers can fail safe. Soft gates
    (phase review) catch and skip; `finish --require-evidence` catches and BLOCKs
    rather than silently skipping the required check."""
    specs = SpecStore(Path(workspace_root) / "specs")
    wi_id = getattr(task, "work_item_id", None)
    if wi_id is not None:
        wi = WorkItemStore(Path(workspace_root) / ".mothership" / "workitems").get(wi_id)
        if wi is not None and wi.spec_id:
            return specs.find_by_id(wi.spec_id)
    candidates = [
        s for s in specs.list()
        if s.task_slug == task.slug and s.status in APPROVED_STATUSES
    ]
    return candidates[0] if len(candidates) == 1 else None


def _docs_dir(workspace_root: Path) -> str:
    """`docs_dir` from mothership.yaml; fall back to "docs" on any load error
    (e.g. running outside a materialized workspace)."""
    from mship.core.config import ConfigLoader
    try:
        return ConfigLoader.load(
            Path(workspace_root) / "mothership.yaml", require_paths=False
        ).docs_dir
    except Exception:
        return "docs"


def _feature_has_plan(wi: WorkItem, task, workspace_root: Path) -> bool:
    """A feature's plan resolves (via the WorkItem's explicit `plan_path` or the
    `<docs_dir>/plans/<date>-<slug>.md` convention) AND carries a mship:task
    anchor."""
    from mship.core.plan import plan_has_tasks, resolve_plan_path

    p = resolve_plan_path(
        task.slug, getattr(wi, "plan_path", None), workspace_root, _docs_dir(workspace_root)
    )
    if p is None:
        return False
    try:
        return plan_has_tasks(p.read_text())
    except (OSError, UnicodeDecodeError):
        # resolved but unreadable — deleted/permission (OSError) or a non-UTF-8
        # / binary file (UnicodeDecodeError). Treat as no valid plan so the gate
        # fails with its actionable message, NOT the generic corrupt-store error.
        return False


def log_hotfix(workspace_root: Path, where: str, task_slug: str) -> None:
    """Record a `--hotfix` gate override to `.mothership/bypass-log.jsonl` via the
    shared bypass log (core/gate.py::record_bypass). `where` identifies the gate
    site (e.g. "dev", "finish"); `task_slug` identifies the task being bypassed —
    record_bypass's log schema is (op, branch, reason), so those are threaded in
    as (where, task_slug, "hotfix") respectively."""
    record_bypass(Path(workspace_root), op=where, branch=task_slug, reason="hotfix")

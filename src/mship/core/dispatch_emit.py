"""Derive the full subagent prompt from a DispatchRecord (spec mship-dispatch-v2).

Run by the SUBAGENT (not the controller): the plan slice is re-parsed from the
canonical plan file and acceptance text is pulled live from the spec store, so
neither is ever copied into the record and both reflect edits at emit time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mship.core.dispatch import (
    BaseShaInfo,
    build_dispatch_prompt,
    extract_plan_task_meta,
)
from mship.core.log import LogEntry
from mship.core.sdd_store import DispatchRecord
from mship.core.state import Task


@dataclass
class PlanDriftWarning:
    plan_path: str
    record_created_at: str
    plan_mtime: str

    def __str__(self) -> str:
        return (
            f"plan {self.plan_path} modified after this dispatch was recorded "
            f"({self.plan_mtime} > {self.record_created_at}); emitting the CURRENT "
            f"plan text — re-dispatch if the task was re-scoped"
        )


def resolve_instruction_and_acs(
    rec: DispatchRecord, workspace_root: Path
) -> tuple[str, list[str], list]:
    """Return (instruction_text, ac_ids, warnings) — plan-sliced or ad-hoc."""
    warnings: list = []
    if rec.plan_task_id is None:
        return rec.instruction or "", list(rec.acs), warnings
    plan_file = workspace_root / rec.plan_path
    text, meta = extract_plan_task_meta(plan_file.read_text(), rec.plan_task_id)
    mtime = datetime.fromtimestamp(plan_file.stat().st_mtime, tz=timezone.utc)
    if mtime > rec.created_at:
        warnings.append(PlanDriftWarning(
            plan_path=str(plan_file),
            record_created_at=rec.created_at.isoformat(),
            plan_mtime=mtime.isoformat(),
        ))
    return text, meta.get("acs", list(rec.acs)), warnings


def resolve_acceptance(ac_ids: list[str], spec) -> tuple[list[tuple[str, str]] | None, list]:
    """Map AC ids to live (id, text) pairs from the spec. An unknown AC id,
    or ids with spec=None, appends a string warning (never an error)."""
    warnings: list = []
    if not ac_ids:
        return None, warnings
    if spec is None:
        warnings.append(
            f"acceptance criteria {', '.join(ac_ids)} referenced but no spec "
            f"is bound to this task — emitting without AC text"
        )
        return None, warnings
    by_id = {ac.id: ac.text for ac in spec.acceptance_criteria}
    acceptance = [(i, by_id[i]) for i in ac_ids if i in by_id]
    unknown = [i for i in ac_ids if i not in by_id]
    if unknown:
        warnings.append(
            f"AC id(s) {', '.join(unknown)} not found in spec {spec.id!r} "
            f"— emitting without them"
        )
    return acceptance, warnings


def _minimal_task(rec: DispatchRecord) -> Task:
    """Reconstruct just what build_dispatch_prompt reads from a Task.

    Used only when the caller (a bare library user or test) doesn't pass the
    real Task: slug/worktree/repo come from the record; the branch is probed
    from the worktree's HEAD ("?" when the worktree is gone or detached probes
    fail); depends_on is empty — the CLI passes the real Task, so dependency
    context is only missing on this fallback path.
    """
    from mship.core.dispatch import _git_out

    branch = _git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=Path(rec.worktree)) or "?"
    return Task(
        slug=rec.task_slug, description="", phase="dev",
        created_at=rec.created_at, affected_repos=[rec.repo],
        worktrees={rec.repo: Path(rec.worktree)}, branch=branch,
        active_repo=rec.repo,
    )


def _snapshot_base_sha_info(rec: DispatchRecord) -> BaseShaInfo:
    """Dispatch-time snapshot fallback when the caller doesn't re-probe git."""
    return BaseShaInfo(
        base_sha=rec.base_sha, origin_base_sha=None,
        head_sha=rec.head_sha or "?", ahead_of_base=None,
        base_behind_origin=None, has_upstream=False,
        summary="as recorded at dispatch time (not re-probed)",
    )


def build_emitted_prompt(
    rec: DispatchRecord,
    *,
    workspace_root: Path,
    spec,
    task: Task | None = None,
    journal_entries: list[LogEntry] | None = None,
    base_sha_info: BaseShaInfo | None = None,
    base_branch: str | None = None,
    agents_md_path: Path | None = None,
    pkg_skills_source: Path | None = None,
    state=None,
    docs_dir: str = "docs",
) -> tuple[str, list]:
    """Return (prompt, warnings) — the full dispatch prompt derived live.

    Contract: `rec`, `workspace_root`, and `spec` are the only required inputs;
    the remaining keyword params mirror build_dispatch_prompt and default to
    record-derived values so a bare record is enough to emit:

    - task: the real Task when available (the CLI passes it); else a minimal
      Task reconstructed from the record (_minimal_task) — no depends_on.
    - journal_entries: default [] (no journal context).
    - base_sha_info: default the record's dispatch-time snapshot; the CLI
      passes a live collect_base_sha_info() probe instead.
    - base_branch: default rec.base_branch.
    - pkg_skills_source: default the installed package's skills dir.

    `spec` supplies live acceptance text for the record's AC ids. An unknown
    AC id, or acs with spec=None, appends a string warning (never an error).

    - docs_dir: workspace docs directory, used only to locate the product
      assumptions store (see below). Default "docs".

    Plan-phase tasks (`task.phase == "plan"`) get the product assumptions
    table auto-appended (auto-seeded if absent) so the planner receives the
    rows to disposition without a separate fetch. Other phases are unaffected.
    """
    instruction, ac_ids, warnings = resolve_instruction_and_acs(rec, workspace_root)
    acceptance, ac_warnings = resolve_acceptance(ac_ids, spec)
    warnings.extend(ac_warnings)

    if pkg_skills_source is None:
        from mship.core.skill_install import pkg_skills_source as _pkg_src
        pkg_skills_source = _pkg_src()

    prompt = build_dispatch_prompt(
        task=task if task is not None else _minimal_task(rec),
        repo=rec.repo,
        instruction=instruction,
        journal_entries=journal_entries if journal_entries is not None else [],
        base_sha_info=base_sha_info if base_sha_info is not None else _snapshot_base_sha_info(rec),
        base_branch=base_branch if base_branch is not None else rec.base_branch,
        agents_md_path=agents_md_path,
        pkg_skills_source=pkg_skills_source,
        state=state,
        mode=rec.mode,
        model=rec.model,
        acceptance=acceptance,
    )

    resolved_task = task if task is not None else _minimal_task(rec)
    prompt = append_plan_assumptions(prompt, resolved_task, workspace_root, docs_dir)

    return prompt, warnings


def append_plan_assumptions(
    prompt: str, task, workspace_root: Path, docs_dir: str = "docs"
) -> str:
    """Append the rendered product-assumptions table to a PLAN-phase task's
    prompt (deterministic L2 injection); no-op for other phases.

    Shared by `build_emitted_prompt` (`--emit`) AND the `--full` inline dispatch
    path (`cli/dispatch.py`) so EVERY plan-dispatch route injects — otherwise a
    `--full` dispatch silently omits the assumptions (Greptile #450). May raise
    for an encrypted store without a key / a malformed store; callers surface it
    cleanly (they catch OSError/ValueError/key errors)."""
    if task.phase != "plan":
        return prompt
    from mship.core.assumptions import AssumptionStore, resolve_mode

    store = AssumptionStore(
        workspace_root, docs_dir=docs_dir, mode=resolve_mode(workspace_root)
    )
    store.seed()
    # render() already carries the "## Assumptions to disposition" header.
    return prompt + "\n\n" + store.render()

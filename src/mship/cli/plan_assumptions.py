"""`mship plan assumptions` sub-group: the cold-checker workflow for plan
assumption coverage (product-assumptions-wave-3a, #444).

Two verbs:

- `check --emit` prints a fixed-shape prompt — the original request/spec
  text, the current assumption rows, and the finished plan text, plus an
  instruction to return per-row verdicts as JSON. `mship` never calls an LLM
  itself; it only builds and prints this prompt. Deliberately excludes the
  journal, any reasoning trace, and codegraph context — the checker judges
  the plan cold, the same way a reviewer with no session history would.
- `result --from-json` ingests those verdicts, deterministically cross-checks
  them against the plan/task text (`core.plan_check.cross_check`), and saves
  a `PlanCheckResult` keyed to the plan's hash.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output

_PROMPT_INSTRUCTION = (
    "Judge this plan cold, using only the request, the assumption rows, and "
    "the finished plan shown above -- nothing else about how or why the work "
    "was done. For each assumption row, return a JSON array of objects "
    'shaped {"axis": <axis name>, "verdict": "covered"|"not-covered"|"n-a", '
    '"reason": <one line>}, judging strictly on whether the finished plan '
    "actually addresses that axis."
)


def _request_text(workspace_root: Path, task_obj) -> str:
    """Original request/spec text: the bound spec's title+body when the task
    has one, else the task's free-text description."""
    if task_obj.spec_id:
        from mship.core.spec_store import SPECS_DIRNAME, SpecStore

        store = SpecStore(workspace_root / SPECS_DIRNAME)
        spec = store.read_strict(task_obj.spec_id)
        if spec is not None:
            return f"{spec.title}\n\n{spec.body}".strip()
    return task_obj.description


def _resolve_common(get_container, task: Optional[str], plan: Optional[str], output: Output):
    """Shared setup for both verbs: workspace_root/docs_dir (mirrors
    cli/plan.py's check-assumptions), the resolved Task (via the same
    resolver every state-changing verb uses), and the resolved plan path."""
    from mship.core.config import ConfigLoader
    from mship.core.plan import effective_plan_path

    container = get_container()
    config_path = Path(container.config_path())
    workspace_root = config_path.parent
    docs_dir = (
        ConfigLoader.load(config_path, require_paths=False).docs_dir
        if config_path.is_file()
        else "docs"
    )

    state = container.state_manager().load()
    resolved = resolve_for_command("plan assumptions", state, task, output)
    task_obj = resolved.task

    # Resolve the plan the SAME way the gate and serve do (WorkItem plan_path,
    # else convention) so the recorded plan_hash matches theirs — a linked-plan
    # WorkItem was otherwise mis-gated (Wave 3a review).
    plan_path = effective_plan_path(task_obj, workspace_root, docs_dir, cli_plan=plan)
    if plan_path is None:
        where = f"for task {task_obj.slug!r}" if plan is None else f"at {plan!r}"
        output.error(f"No plan found {where}.")
        raise typer.Exit(1)

    return container, workspace_root, docs_dir, task_obj, plan_path


def register(plan_app: typer.Typer, get_container):
    assumptions_app = typer.Typer(
        name="assumptions",
        help="Cold-checker prompt for plan assumption coverage, and its results.",
        no_args_is_help=True,
    )

    @assumptions_app.command("check")
    def check(
        task: Optional[str] = typer.Option(
            None, "--task", help="Task slug; resolved like other state-changing commands (cwd/MSHIP_TASK fallback)."
        ),
        plan: Optional[str] = typer.Option(None, "--plan", help="Explicit plan path (workspace-relative)."),
        emit: bool = typer.Option(False, "--emit", help="Print the cold-checker prompt to stdout."),
    ):
        """Print the cold-checker prompt for `mship plan assumptions result` to
        consume. mship does not call an LLM itself."""
        from mship.core.assumptions import AssumptionStore, resolve_mode

        output = Output()
        if not emit:
            output.error("`mship plan assumptions check` requires --emit.")
            raise typer.Exit(1)

        _container, workspace_root, docs_dir, task_obj, plan_path = _resolve_common(
            get_container, task, plan, output
        )

        request_text = _request_text(workspace_root, task_obj)
        store = AssumptionStore(workspace_root, docs_dir=docs_dir, mode=resolve_mode(workspace_root))
        rows_block = store.render()
        plan_text = plan_path.read_text()

        prompt = (
            "# Original request/spec\n\n"
            f"{request_text}\n\n"
            f"{rows_block}\n"
            "# Finished plan\n\n"
            f"{plan_text}\n\n"
            "# Instruction\n\n"
            f"{_PROMPT_INSTRUCTION}\n"
        )
        output.print(prompt)

    @assumptions_app.command("result")
    def result(
        from_json: str = typer.Option(
            ..., "--from-json", help="Path to the checker's verdicts JSON, or - for stdin."
        ),
        task: Optional[str] = typer.Option(
            None, "--task", help="Task slug; resolved like other state-changing commands (cwd/MSHIP_TASK fallback)."
        ),
        plan: Optional[str] = typer.Option(None, "--plan", help="Explicit plan path (workspace-relative)."),
    ):
        """Ingest per-axis checker verdicts, deterministically cross-check them
        against the plan/task text, and save the combined result."""
        import json
        import sys

        from pydantic import ValidationError

        from mship.core.assumptions import AssumptionStore, resolve_mode
        from mship.core.plan_check import (
            AxisVerdict,
            PlanCheckResult,
            PlanCheckStore,
            cross_check,
            flags_from_verdicts,
            plan_hash,
        )

        output = Output()
        container, workspace_root, docs_dir, task_obj, plan_path = _resolve_common(
            get_container, task, plan, output
        )

        if from_json == "-":
            raw = sys.stdin.read()
        else:
            try:
                raw = Path(from_json).read_text()
            except OSError as e:
                output.error(f"Cannot read --from-json {from_json!r}: {e}")
                raise typer.Exit(1)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            output.error(f"Invalid JSON from --from-json: {e}")
            raise typer.Exit(1)

        try:
            verdicts = [AxisVerdict(**item) for item in payload]
        except (TypeError, ValidationError) as e:
            output.error(f"Invalid verdicts JSON from --from-json: {e}")
            raise typer.Exit(1)

        store = AssumptionStore(workspace_root, docs_dir=docs_dir, mode=resolve_mode(workspace_root))
        rows = store.load()
        plan_text = plan_path.read_text()
        request_text = _request_text(workspace_root, task_obj)

        flags = flags_from_verdicts(verdicts, rows) + cross_check(
            verdicts, rows,
            plan_text=plan_text,
            task_text=request_text,
            affected_repos=list(task_obj.affected_repos),
        )

        new_hash = plan_hash(plan_text)
        # Carry over prior approvals when RE-checking the SAME plan (unchanged
        # hash): re-running the checker to refresh a verdict shouldn't silently
        # wipe sign-offs. A CHANGED plan (different hash) correctly drops them —
        # an approval against the old plan mustn't survive an edit (Wave 3a review).
        pcstore = PlanCheckStore(workspace_root / ".mothership")
        # Lock spans read-prior → merge approvals → save so a concurrent approve
        # (or a second result) can't clobber this write (Greptile #451).
        with pcstore.transaction(task_obj.slug):
            prior = pcstore.get(task_obj.slug)
            if prior is not None and prior.plan_hash == new_hash:
                # Key on (axis, source) only — NOT the checker's free-text reason.
                # (axis, source) is unique per check run (one checker flag per axis,
                # one cross-check flag per axis), and the human signed off on "this
                # axis, for this plan version" — the plan_hash already pins the
                # version. Including the LLM-paraphrased reason would silently drop a
                # real sign-off when the checker re-runs and re-words the same gap.
                approved = {
                    (f.axis, f.source): f for f in prior.flags if f.approved
                }
                for f in flags:
                    keep = approved.get((f.axis, f.source))
                    if keep is not None:
                        f.approved = True
                        f.approved_by = keep.approved_by
                        f.approved_reason = keep.approved_reason

            check_result = PlanCheckResult(
                task_slug=task_obj.slug,
                plan_hash=new_hash,
                verdicts=verdicts,
                flags=flags,
            )
            pcstore.save(check_result)

        pending = sum(1 for f in flags if not f.approved)

        if output.human_mode:
            output.success(
                f"Saved plan-check for {task_obj.slug}: {len(flags)} flag(s), {pending} pending."
            )
        else:
            output.json({
                "task": task_obj.slug,
                "plan_hash": check_result.plan_hash,
                "verdicts": [v.model_dump() for v in verdicts],
                "flags": [f.model_dump() for f in flags],
                "pending": pending,
            })

    @assumptions_app.command("status")
    def status(
        task: Optional[str] = typer.Option(
            None, "--task", help="Task slug; resolved like other state-changing commands (cwd/MSHIP_TASK fallback)."
        ),
        plan: Optional[str] = typer.Option(None, "--plan", help="Explicit plan path (workspace-relative)."),
    ):
        """Report whether the stored plan-check for this task is fresh (plan
        unchanged since the check), stale (plan edited after), or absent."""
        from mship.core.plan_check import PlanCheckStore, plan_hash

        output = Output()
        container, workspace_root, _docs_dir, task_obj, plan_path = _resolve_common(
            get_container, task, plan, output
        )

        stored = PlanCheckStore(workspace_root / ".mothership").get(task_obj.slug)
        if stored is None:
            if output.human_mode:
                output.print(f"No stored plan-check for {task_obj.slug}.")
            else:
                output.json({"task": task_obj.slug, "fresh": False, "pending": 0, "flags": []})
            return

        fresh = stored.plan_hash == plan_hash(plan_path.read_text())
        pending = sum(1 for f in stored.flags if not f.approved)

        if output.human_mode:
            state = "fresh" if fresh else "stale"
            output.print(f"{task_obj.slug}: {state}, {pending} pending flag(s).")
            for f in stored.flags:
                mark = "approved" if f.approved else "pending"
                output.print(f"  [{mark}] {f.axis} ({f.source}): {f.reason}")
        else:
            output.json({
                "task": task_obj.slug,
                "fresh": fresh,
                "pending": pending,
                "flags": [f.model_dump() for f in stored.flags],
            })

    @assumptions_app.command("approve")
    def approve(
        axis: str = typer.Argument(..., help="Axis name of the pending flag to approve."),
        reason: Optional[str] = typer.Option(None, "--reason", help="Why this flag is approved."),
        task: Optional[str] = typer.Option(
            None, "--task", help="Task slug; resolved like other state-changing commands (cwd/MSHIP_TASK fallback)."
        ),
        plan: Optional[str] = typer.Option(None, "--plan", help="Explicit plan path (workspace-relative)."),
    ):
        """Mark the matching pending flag as approved (human sign-off) and
        re-save. The only way a flag clears."""
        import os

        from mship.core.plan import _normalize_axis
        from mship.core.plan_check import PlanCheckStore

        output = Output()
        container, workspace_root, _docs_dir, task_obj, _plan_path = _resolve_common(
            get_container, task, plan, output
        )

        store = PlanCheckStore(workspace_root / ".mothership")
        # Lock spans get → mutate → save so a concurrent approve/result can't
        # overwrite this sign-off (Greptile #451).
        with store.transaction(task_obj.slug):
            stored = store.get(task_obj.slug)
            if stored is None:
                output.error(f"No stored plan-check for {task_obj.slug}.")
                raise typer.Exit(1)

            target_axis = _normalize_axis(axis)
            match = next(
                (f for f in stored.flags if not f.approved and _normalize_axis(f.axis) == target_axis),
                None,
            )
            if match is None:
                output.error(f"No pending flag for axis {axis!r} on {task_obj.slug}.")
                raise typer.Exit(1)

            match.approved = True
            match.approved_reason = reason
            match.approved_by = os.environ.get("USER") or "unknown"
            store.save(stored)

        pending = sum(1 for f in stored.flags if not f.approved)

        if output.human_mode:
            output.success(f"Approved {axis!r} for {task_obj.slug}: {pending} pending flag(s) remain.")
        else:
            output.json({
                "task": task_obj.slug,
                "axis": match.axis,
                "pending": pending,
                "flags": [f.model_dump() for f in stored.flags],
            })

    plan_app.add_typer(assumptions_app)

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
    from mship.core.plan import resolve_plan_path

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

    plan_path = resolve_plan_path(task_obj.slug, plan, workspace_root, docs_dir)
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

        flags = flags_from_verdicts(verdicts) + cross_check(
            verdicts, rows,
            plan_text=plan_text,
            task_text=request_text,
            affected_repos=list(task_obj.affected_repos),
        )

        check_result = PlanCheckResult(
            task_slug=task_obj.slug,
            plan_hash=plan_hash(plan_text),
            verdicts=verdicts,
            flags=flags,
        )
        PlanCheckStore(Path(container.state_dir())).save(check_result)

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

    plan_app.add_typer(assumptions_app)

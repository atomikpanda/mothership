"""`mship dispatch` — emit an agent-agnostic subagent prompt to stdout.

See docs/superpowers/specs/2026-04-17-mship-dispatch-design.md.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output
from mship.core import dispatch as _d
from mship.core.base_resolver import resolve_base
from mship.core.plan import resolve_plan_path
from mship.core.skill_install import pkg_skills_source
from mship.core.workitem_store import WorkItemStore


def _resolve_task_plan(container, task_obj) -> Optional[Path]:
    """Auto-resolve the implementation plan for a task when `--plan` is omitted.

    Precedence: the task's WorkItem `plan_path` (explicit link) wins, else the
    `<docs_dir>/plans/<slug>.md` discovery convention. Returns None when nothing
    resolves.
    """
    workspace_root = Path(container.config_path()).parent
    docs_dir = getattr(container.config(), "docs_dir", "docs")
    linked = None
    work_item_id = getattr(task_obj, "work_item_id", None)
    if work_item_id:
        wi = WorkItemStore(Path(container.state_dir()) / "workitems").get(work_item_id)
        if wi is not None:
            linked = wi.plan_path
    return resolve_plan_path(task_obj.slug, linked, workspace_root, docs_dir)


def register(app: typer.Typer, get_container):
    @app.command(rich_help_panel="Inspection")
    def dispatch(
        task: Optional[str] = typer.Option(None, "--task", help="Target task slug (defaults to cwd-resolved)."),
        repo: Optional[str] = typer.Option(None, "--repo", help="Which repo's worktree to target (multi-repo tasks)."),
        instruction: Optional[str] = typer.Option(
            None, "--instruction", "-i",
            help='Instruction text passed verbatim. Use "-" to read it from stdin.',
        ),
        plan: Optional[Path] = typer.Option(
            None, "--plan", help="Path to an implementation plan with anchored task blocks."
        ),
        plan_task: Optional[str] = typer.Option(
            None, "--plan-task",
            help="Anchor id in --plan to use as the instruction (mutually exclusive with --instruction).",
        ),
        mode: str = typer.Option(
            "implementer", "--mode",
            help=(
                "Closing framing. 'implementer' (default): scope to a single task, "
                "report back, do not open a PR — for per-task execution under an "
                "orchestrator that owns finishing. 'standalone': finish the work and "
                "open the PR via `mship finish`."
            ),
        ),
    ):
        """Emit a self-contained markdown subagent prompt to stdout.

        Exactly one instruction source is required: inline `--instruction "<text>"`,
        stdin `--instruction -`, or `--plan-task <id>`. With `--plan-task` and no
        `--plan`, the task's implementation plan is auto-resolved (its WorkItem's
        linked `plan_path`, else the `<docs_dir>/plans/<slug>.md` convention); an
        explicit `--plan <path>` overrides that.
        """
        output = Output()

        if mode not in _d.DISPATCH_MODES:
            output.error(
                f"--mode must be one of: {', '.join(_d.DISPATCH_MODES)} (got {mode!r})."
            )
            raise typer.Exit(code=2)

        # --- resolve the instruction source (exactly one of) ---
        if (instruction is not None) == (plan_task is not None):
            output.error(
                'provide exactly one instruction source: --instruction "<text>", '
                "--instruction - (stdin), or --plan-task <id> (with --plan, or "
                "auto-resolved from the task's plan)."
            )
            raise typer.Exit(code=2)

        # --plan is only meaningful with --plan-task; reject it rather than
        # silently discarding the plan (e.g. `--plan x --instruction "..."`).
        if plan is not None and plan_task is None:
            output.error("--plan requires --plan-task <id>.")
            raise typer.Exit(code=2)

        container = get_container()
        state = container.state_manager().load()
        resolved = resolve_for_command("dispatch", state, task, output)
        task_obj = resolved.task

        if plan_task is not None:
            # Explicit --plan wins; else auto-resolve the task's linked/discovered
            # plan (a resolved plan is the single instruction source).
            plan_path = plan
            if plan_path is None:
                plan_path = _resolve_task_plan(container, task_obj)
                if plan_path is None:
                    output.error(
                        f"no implementation plan found for task {task_obj.slug!r} to "
                        f"resolve --plan-task {plan_task!r}. Pass --plan <path>, link "
                        f"one with `mship item link-plan`, or write one at "
                        f"<docs_dir>/plans/<date>-{task_obj.slug}.md (writing-plans)."
                    )
                    raise typer.Exit(code=2)
            try:
                plan_text = plan_path.read_text()
            except OSError as e:
                output.error(f"cannot read plan {str(plan_path)!r}: {e}")
                raise typer.Exit(code=2)
            try:
                resolved_instruction = _d.extract_plan_task(plan_text, plan_task)
            except ValueError as e:
                output.error(str(e))
                raise typer.Exit(code=2)
        elif instruction == "-":
            resolved_instruction = sys.stdin.read().strip()
            if not resolved_instruction:
                output.error("no instruction read from stdin.")
                raise typer.Exit(code=2)
        else:
            resolved_instruction = instruction  # inline (guaranteed non-None here)

        try:
            resolved_repo = _d.resolve_repo(task_obj, repo)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        worktree = Path(task_obj.worktrees[resolved_repo])

        config = container.config()
        repo_config = config.repos.get(resolved_repo)
        effective_base = resolve_base(
            resolved_repo, repo_config, cli_base=None, base_map={},
            known_repos=config.repos.keys(), task_base=task_obj.base_override,
        ) or task_obj.base_branch or "main"
        base_sha_info = _d.collect_base_sha_info(worktree, effective_base)

        log_mgr = container.log_manager()
        journal_entries = log_mgr.read(task_obj.slug, last=10)

        # AGENTS.md lives next to the config file (workspace root).
        config_path = Path(container.config_path())
        agents_md = config_path.parent / "AGENTS.md"
        agents_md_path = agents_md if agents_md.is_file() else None

        prompt = _d.build_dispatch_prompt(
            task=task_obj,
            repo=resolved_repo,
            instruction=resolved_instruction,
            journal_entries=journal_entries,
            base_sha_info=base_sha_info,
            base_branch=effective_base,
            agents_md_path=agents_md_path,
            pkg_skills_source=pkg_skills_source(),
            state=state,
            mode=mode,
        )
        # Print directly to stdout (NOT via Output.json — this is meant to be piped).
        print(prompt)

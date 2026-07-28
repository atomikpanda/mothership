"""`mship dispatch` — emit an agent-agnostic subagent prompt to stdout.

See docs/superpowers/specs/2026-04-17-mship-dispatch-design.md.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output
from mship.core import dispatch as _d
from mship.core.base_resolver import resolve_base
from mship.core.dispatch_emit import build_emitted_prompt
from mship.core.dispatch_models import resolve_model
from mship.core.dispatch_stub import build_stub
from mship.core.sdd_store import DispatchRecord, SddStore
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


def _journal_and_agents(container, slug: str) -> tuple[list, Optional[Path]]:
    """Gather the journal tail and the workspace AGENTS.md (if present) —
    shared by the full-prompt and --emit paths."""
    journal_entries = container.log_manager().read(slug, last=10)
    # AGENTS.md lives next to the config file (workspace root).
    agents_md = Path(container.config_path()).parent / "AGENTS.md"
    return journal_entries, agents_md if agents_md.is_file() else None


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
        model: Optional[str] = typer.Option(
            None, "--model",
            help="Model for the subagent. Default: dispatch_models map in "
                 "mothership.yaml, else built-in per-mode default.",
        ),
        full: bool = typer.Option(
            False, "--full",
            help="Print the full subagent prompt inline (legacy). Default for "
                 "--plan-task is a closed stub; the subagent emits its own "
                 "prompt via --emit.",
        ),
        stub: bool = typer.Option(
            False, "--stub",
            help="Print the closed stub even for --instruction dispatches.",
        ),
        emit: bool = typer.Option(
            False, "--emit",
            help="Subagent-side: derive and print MY full prompt from the "
                 "dispatch record (cwd-resolved task). Prints drift warnings "
                 "to stderr.",
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

        # --- subagent-side emit: derive the prompt from the persisted record ---
        if emit:
            if instruction is not None or plan is not None or plan_task is not None:
                output.error(
                    "--emit derives everything from the dispatch record; drop "
                    "--instruction/--plan/--plan-task."
                )
                raise typer.Exit(code=2)
            container = get_container()
            state = container.state_manager().load()
            resolved = resolve_for_command("dispatch", state, task, output)
            task_obj = resolved.task
            rec = SddStore(Path(container.state_dir())).find_for_slug(task_obj.slug)
            if rec is None:
                output.error(
                    f"no dispatch record for task {task_obj.slug!r} — the "
                    f"controller runs `mship dispatch --plan-task N` first."
                )
                raise typer.Exit(code=1)
            spec = None
            if rec.acs or rec.plan_task_id is not None:
                spec_id = getattr(task_obj, "spec_id", None)
                if spec_id:
                    from mship.core.spec_store import SPECS_DIRNAME, SpecStore
                    spec = SpecStore(
                        Path(container.config_path()).parent / SPECS_DIRNAME
                    ).find_by_id(spec_id)
            workspace_root = Path(container.config_path()).parent
            journal_entries, agents_md_path = _journal_and_agents(container, task_obj.slug)
            base_sha_info = _d.collect_base_sha_info(Path(rec.worktree), rec.base_branch)
            try:
                prompt, warnings = build_emitted_prompt(
                    rec,
                    workspace_root=workspace_root,
                    spec=spec,
                    task=task_obj,
                    journal_entries=journal_entries,
                    base_sha_info=base_sha_info,
                    base_branch=rec.base_branch,
                    agents_md_path=agents_md_path,
                    pkg_skills_source=pkg_skills_source(),
                    state=state,
                )
            except (OSError, ValueError) as e:
                output.error(f"cannot derive the prompt from the record: {e}")
                raise typer.Exit(code=1)
            for w in warnings:
                print(f"warning: {w}", file=sys.stderr)
            print(prompt)
            return

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
                resolved_instruction, plan_meta = _d.extract_plan_task_meta(plan_text, plan_task)
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

        resolved_model = resolve_model(
            mode, flag=model, configured=config.dispatch_models
        )

        # Persist the record (pointer + metadata; never plan prose).
        acs = plan_meta.get("acs", []) if plan_task is not None else []
        rec = DispatchRecord(
            task_slug=task_obj.slug,
            work_item_id=getattr(task_obj, "work_item_id", None),
            mode=mode,
            model=resolved_model,
            repo=resolved_repo,
            worktree=str(worktree),
            base_branch=effective_base,
            base_sha=base_sha_info.base_sha,
            head_sha=base_sha_info.head_sha,
            # Resolved (absolute) so emit reads exactly the file extracted from
            # here — an explicit relative --plan recorded as-typed would depend
            # on the emit-time cwd instead.
            plan_path=str(plan_path.resolve()) if plan_task is not None else None,
            plan_task_id=plan_task,
            acs=acs,
            instruction=None if plan_task is not None else resolved_instruction,
            created_at=datetime.now(timezone.utc),
        )
        record_path = SddStore(Path(container.state_dir())).write(rec)

        want_stub = (plan_task is not None and not full) or stub
        if want_stub:
            print(build_stub(rec, record_path=str(record_path)), end="")
            return

        journal_entries, agents_md_path = _journal_and_agents(container, task_obj.slug)

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

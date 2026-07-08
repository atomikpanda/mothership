from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

import typer

from mship.cli.output import Output
from mship.core.dispatch import collect_base_sha_info
from mship.core.message_store import MessageStore
from mship.core.run_state import RunStateRepo
from mship.core.runner import BranchState, RunDeps, checkpoint_bail, run_once
from mship.core.spec_body import parse_body_sections
from mship.core.spec_store import SPECS_DIRNAME, SpecStore
from mship.core.workitem import ExternalLink, Kind, Phase, Provider
from mship.core.workitem_store import WorkItemStore
from mship.core.view.workitem_index import build_workitem_index


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _run_holder() -> str:
    """Opaque, deterministic-per-process claim token: host + pid.

    Two invocations in the same process share it (so a run-next claim can be
    released by a later bail); different hosts/processes never collide."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _workspace_origin(workspace_root: Path) -> str:
    """The git remote the run-state ref lives on — the workspace repo's ``origin``.

    Unattended runs coordinate through a ``mship-run-state`` orphan branch on this
    remote (see core/run_state.py). Falls back to the workspace root path when no
    origin is configured so single-host/local use still works; a real unattended
    deployment needs a pushable origin for cross-run coordination."""
    r = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(workspace_root), capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return str(workspace_root)


def _base_prompt_for(item, spec) -> str:
    """The base (pre-resumable-wrap) dispatch prompt for an unattended run.

    v1 is spec-first: it renders the approved spec's Problem + acceptance criteria so
    the host agent has the intent. ``run_once`` wraps this with a RESUMING preamble
    when the item's branch already has commits (see run_dispatch.resumable_dispatch)."""
    title = spec.title if spec is not None else item.title
    lines = [f"# Unattended run: {title}", "", f"work item: {item.id}"]
    if spec is not None:
        lines.append(f"spec: {spec.id}")
        problem = parse_body_sections(spec.body).get("Problem", "").strip()
        if problem:
            lines += ["", "## Problem", "", problem]
        if spec.acceptance_criteria:
            lines += ["", "## Acceptance criteria", ""]
            lines += [f"- [{ac.id}] {ac.text}" for ac in spec.acceptance_criteria]
    lines += ["",
              "Implement this work item to satisfy its approved spec, then finish per "
              "workspace conventions (mship test until green, then mship finish)."]
    return "\n".join(lines)


def _branch_state_for(item, state, log_mgr, config) -> BranchState:
    """Per-item git/branch facts ``run_once`` needs for the resumable wrap.

    Uses the item's first linked task when present (its branch + recent journal, plus
    a git probe for commits-ahead when a worktree exists on disk); otherwise a fresh
    start on the configured branch pattern (commits_ahead=0 ⇒ no RESUMING preamble)."""
    task = next((state.tasks[s] for s in item.task_slugs if s in state.tasks), None)
    if task is None:
        return BranchState(branch=config.branch_pattern.format(slug=item.id),
                           commits_ahead=0, recent_journal=[])
    journal = [e.message.splitlines()[0]
               for e in log_mgr.read(task.slug, last=5) if e.message]
    commits_ahead = 0
    worktree = next((Path(p) for p in task.worktrees.values() if Path(p).is_dir()), None)
    if worktree is not None:
        commits_ahead = collect_base_sha_info(
            worktree, task.base_branch or "main").ahead_of_base or 0
    return BranchState(branch=task.branch, commits_ahead=commits_ahead,
                       recent_journal=journal)


def register(parent: typer.Typer, get_container) -> None:
    item_app = typer.Typer(help="First-class work items (the phase-aware cockpit spine).")
    parent.add_typer(item_app, name="item")

    def _ctx():
        container = get_container()
        state_dir = Path(container.state_dir())
        workspace_root = Path(container.config_path()).parent
        return (
            WorkItemStore(state_dir / "workitems"),
            SpecStore(workspace_root / SPECS_DIRNAME),
            container.state_manager(),
            MessageStore(state_dir / "messages"),
            container.config().workspace,
        )

    @item_app.command("new")
    def new(title: str, kind: str = typer.Option("feature", "--kind",
            help="feature | bug | chore | question")):
        valid_kinds = get_args(Kind)
        if kind not in valid_kinds:
            typer.echo(f"invalid kind: {kind!r} (choose from {', '.join(valid_kinds)})", err=True)
            raise typer.Exit(1)
        items, _, _, _, workspace = _ctx()
        wi = items.create(title=title, kind=kind, workspace=workspace,
                          now=datetime.now(timezone.utc))
        typer.echo(wi.id)

    @item_app.command("list")
    def list_items():
        items, specs, state_manager, msgs, _ = _ctx()
        summaries = build_workitem_index(
            items.list(),
            {s.id: s for s in specs.list()},
            dict(state_manager.load().tasks),
            {t.id: t for t in msgs.list()},
        )
        rows = [{"id": s.id, "title": s.title, "kind": s.kind, "phase": s.phase,
                 "needs_approval": s.attention.needs_approval,
                 "needs_decision": s.attention.needs_decision,
                 "blocked": s.attention.blocked, "needs_review": s.attention.needs_review}
                for s in summaries]
        output = Output()
        if output.json_mode:
            typer.echo(json.dumps(rows))
        else:
            for r in rows:
                flags = "".join(k[0].upper() for k in
                                ("needs_approval", "needs_decision", "blocked", "needs_review")
                                if r[k])
                typer.echo(f"{r['id']}  [{r['phase']}]  {r['title']}  {flags}")
            if not rows:
                typer.echo("(no work items)")

    @item_app.command("show")
    def show(item_id: str):
        items, _, _, _, _ = _ctx()
        wi = items.get(item_id)
        if wi is None:
            typer.echo(f"no work item {item_id!r}", err=True)
            raise typer.Exit(1)
        typer.echo(wi.model_dump_json(indent=2))

    @item_app.command("link-spec")
    def link_spec(item_id: str, spec_id: str):
        items, _, _, _, _ = _ctx()
        _guard(items, item_id)
        items.link_spec(item_id, spec_id, now=datetime.now(timezone.utc))
        typer.echo(f"linked spec {spec_id} -> {item_id}")

    @item_app.command("link-task")
    def link_task(item_id: str, task_slug: str):
        items, _, state_manager, _, _ = _ctx()
        _guard(items, item_id)
        items.add_task(item_id, task_slug, now=datetime.now(timezone.utc), state=state_manager)
        typer.echo(f"linked task {task_slug} -> {item_id}")

    @item_app.command("link-url")
    def link_url(item_id: str, url: str,
                 provider: str = typer.Option("url", "--provider"),
                 title: str = typer.Option("", "--title")):
        valid_providers = get_args(Provider)
        if provider not in valid_providers:
            typer.echo(f"invalid provider: {provider!r} (choose from {', '.join(valid_providers)})", err=True)
            raise typer.Exit(1)
        items, _, _, _, _ = _ctx()
        _guard(items, item_id)
        items.add_external_link(item_id, ExternalLink(provider=provider, url=url, title=title),
                                now=datetime.now(timezone.utc))
        typer.echo(f"linked {provider} url -> {item_id}")

    @item_app.command("phase")
    def phase(item_id: str, phase: str):
        valid_phases = get_args(Phase)
        if phase not in valid_phases:
            typer.echo(f"invalid phase: {phase!r} (choose from {', '.join(valid_phases)})", err=True)
            raise typer.Exit(1)
        items, _, _, _, _ = _ctx()
        _guard(items, item_id)
        items.set_phase_override(item_id, phase, now=datetime.now(timezone.utc))
        typer.echo(f"set phase_override={phase} on {item_id}")

    @item_app.command("unattended")
    def unattended(item_id: str,
                   on: bool = typer.Option(True, "--on/--off",
                       help="Opt this item into (or out of) unattended runs.")):
        items, _, _, _, _ = _ctx()
        _guard(items, item_id)
        items.set_unattended(item_id, on, now=datetime.now(timezone.utc))
        typer.echo(f"{item_id}: unattended={on}")

    def _build_run_deps(holder: str, now) -> RunDeps:
        """Wire the runner's injectable edges to this workspace's real stores.

        The impure edges: run_state → a git-backed RunStateRepo on the workspace
        origin (workdir under the state dir); build_base_prompt → spec-first prompt;
        branch_state → the item's task branch/journal facts; mark_blocked → set the
        item's task(s) blocked_reason (the existing, derived block mechanism). The
        pure ``claimed`` snapshot is empty — ``try_claim`` is the authoritative gate
        (RunStateRepo has no all-claims listing)."""
        container = get_container()
        state_dir = Path(container.state_dir())
        workspace_root = Path(container.config_path()).parent
        config = container.config()
        items = WorkItemStore(state_dir / "workitems")
        specs = SpecStore(workspace_root / SPECS_DIRNAME)
        state_manager = container.state_manager()
        log_mgr = container.log_manager()

        specs_by_id = {s.id: s for s in specs.list()}
        spec_approved = {sid: (s.status == "approved") for sid, s in specs_by_id.items()}
        run_state = RunStateRepo(_workspace_origin(workspace_root), state_dir / "run-state")

        def mark_blocked(item, reason):
            stamp = now()

            def _apply(s):
                for slug in item.task_slugs:
                    if slug in s.tasks:
                        s.tasks[slug].blocked_reason = reason
                        s.tasks[slug].blocked_at = stamp
            state_manager.mutate(_apply)

        return RunDeps(
            items=items.list(),
            spec_approved=spec_approved,
            claimed=set(),
            run_state=run_state,
            build_base_prompt=lambda it: _base_prompt_for(
                it, specs_by_id.get(it.spec_id) if it.spec_id else None),
            branch_state=lambda it: _branch_state_for(
                it, state_manager.load(), log_mgr, config),
            mark_blocked=mark_blocked,
            holder=holder,
            now=now,
        )

    @item_app.command("run-next")
    def run_next():
        """Claim + emit the next eligible unattended item's dispatch prompt.

        The pull API a host adapter calls each tick: selects the oldest eligible item
        (unattended, phase=ready, approved spec, unclaimed), claims it on the shared
        run-state ref, and prints its resumable dispatch prompt (JSON in non-TTY,
        the raw prompt on a terminal). Prints ``{"runnable": false}`` and exits 0
        when nothing is eligible. Nested under ``item`` so it never collides with the
        service-starting top-level ``mship run``."""
        deps = _build_run_deps(holder=_run_holder(), now=_utcnow)
        result = run_once(deps)
        output = Output()
        if result is None:
            if output.json_mode:
                output.json({"runnable": False})
            else:
                typer.echo("no runnable work item")
            return
        if output.json_mode:
            output.json({"runnable": True, "item_id": result.item.id,
                         "prompt": result.prompt})
        else:
            typer.echo(result.prompt)

    @item_app.command("bail")
    def bail(item_id: str,
             reason: str = typer.Option(..., "--reason",
                 help="Why the run bailed (recorded on the run-log and as the block reason).")):
        """Checkpoint-bail a claimed item: log the reason, block it, release the claim.

        Called by the host agent on an unresolvable fork/failure. Leaves the branch
        intact for a later resume; mship owns claim/checkpoint/bail (not the model)."""
        items, _, _, _, _ = _ctx()
        item = items.get(item_id)
        if item is None:
            typer.echo(f"no work item {item_id!r}", err=True)
            raise typer.Exit(1)
        deps = _build_run_deps(holder=_run_holder(), now=_utcnow)
        checkpoint_bail(deps, item, reason)
        output = Output()
        if output.json_mode:
            output.json({"item_id": item_id, "bailed": True, "reason": reason})
        else:
            typer.echo(f"bailed {item_id}: {reason}")

    @item_app.command("migrate")
    def migrate():
        from mship.core.workitem_migrate import wrap_existing
        items, specs, state_manager, msgs, workspace = _ctx()
        created = wrap_existing(items, specs, state_manager, msgs,
                                now=datetime.now(timezone.utc), workspace=workspace)
        typer.echo(f"created {len(created)} work item(s)")

    def _guard(items: WorkItemStore, item_id: str) -> None:
        if items.get(item_id) is None:
            typer.echo(f"no work item {item_id!r}", err=True)
            raise typer.Exit(1)

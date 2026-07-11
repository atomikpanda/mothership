from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

import typer

from mship.cli.output import Output
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


_REF_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _sanitize_ref_token(token: str) -> str:
    """Make an arbitrary string safe to embed as one git ref path component.

    Collapses any run of characters that aren't ``[A-Za-z0-9_-]`` (``/``,
    whitespace, ``.``, ``~^:?*[]`` etc.) to a single ``-`` and trims leading/
    trailing ``-``. Dots are deliberately excluded from the allowed set too —
    keeping them would let a slug like ``a..b`` survive verbatim, and ``..``
    is itself invalid inside a git ref."""
    return _REF_UNSAFE.sub("-", token).strip("-") or "x"


def _probe_ref_names(task) -> tuple[str, str]:
    """Unique throwaway probe ref names for one ``_remote_commits_ahead`` call.

    Greptile #3: the ref names were hardcoded (``refs/mship-probe/branch`` and
    ``.../base``), so two concurrent probes writing into the same clone — two
    tasks sharing an affected repo, or two runner processes racing ``item
    run-next`` — could collide mid-fetch and read/delete each other's refs.
    Namespacing by the task's (sanitized) slug plus this process's pid keeps
    concurrent probes disjoint without needing any external locking."""
    ns = f"{_sanitize_ref_token(task.slug)}-{os.getpid()}"
    return f"refs/mship-probe/{ns}/branch", f"refs/mship-probe/{ns}/base"


def _remote_commits_ahead(task, config) -> int:
    """How many commits the task's branch is ahead of its base ON ORIGIN.

    A resumed unattended run is often a fresh clone with no task worktree on disk
    (AC4, ephemeral hosts), so commits-ahead must come from the *remote* branch a
    prior bail pushed (FIX#4b) — not a local worktree that may not exist. For each
    affected repo's checkout, if origin has the branch, fetch the branch + base into
    throwaway probe refs and count ``base..branch``. Returns the max across repos
    (any repo with prior commits ⇒ resume); 0 when the branch isn't on origin yet
    (a truly fresh start ⇒ no RESUMING preamble). Best-effort: git failures ⇒ 0."""
    base = task.base_branch or "main"
    repos = getattr(config, "repos", {}) or {}
    ahead = 0
    for name in task.affected_repos:
        repo = repos.get(name)
        if repo is None:
            continue
        repo_dir = Path(repo.path)
        if not repo_dir.exists():
            continue
        ls = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", task.branch],
            cwd=str(repo_dir), capture_output=True, text=True,
        )
        if ls.returncode != 0 or not ls.stdout.strip():
            continue  # branch not on origin for this repo (yet)
        br_ref, base_ref = _probe_ref_names(task)
        fetch = subprocess.run(
            ["git", "fetch", "-q", "origin",
             f"+{task.branch}:{br_ref}", f"+{base}:{base_ref}"],
            cwd=str(repo_dir), capture_output=True, text=True,
        )
        if fetch.returncode == 0:
            out = subprocess.run(
                ["git", "rev-list", "--count", f"{base_ref}..{br_ref}"],
                cwd=str(repo_dir), capture_output=True, text=True,
            )
            if out.returncode == 0 and out.stdout.strip().isdigit():
                ahead = max(ahead, int(out.stdout.strip()))
        for ref in (br_ref, base_ref):  # clean up the throwaway probe refs
            subprocess.run(["git", "update-ref", "-d", ref], cwd=str(repo_dir),
                           capture_output=True, text=True)
    return ahead


def _branch_state_for(item, state, log_mgr, config) -> BranchState:
    """Per-item git/branch facts ``run_once`` needs for the resumable wrap.

    Uses the item's first linked task when present (its branch + recent journal, plus
    commits-ahead read from the *remote* branch so a fresh clone still resumes —
    ``_remote_commits_ahead``); otherwise a fresh start on the configured branch
    pattern (commits_ahead=0 ⇒ no RESUMING preamble)."""
    task = next((state.tasks[s] for s in item.task_slugs if s in state.tasks), None)
    if task is None:
        return BranchState(branch=config.branch_pattern.format(slug=item.id),
                           commits_ahead=0, recent_journal=[])
    journal = [e.message.splitlines()[0]
               for e in log_mgr.read(task.slug, last=5) if e.message]
    return BranchState(branch=task.branch,
                       commits_ahead=_remote_commits_ahead(task, config),
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
    def list_items(all_items: bool = typer.Option(False, "--all",
            help="Include archived work items (hidden by default).")):
        items, specs, state_manager, msgs, _ = _ctx()
        summaries = build_workitem_index(
            items.list(include_archived=all_items),
            {s.id: s for s in specs.list()},
            dict(state_manager.load().tasks),
            {t.id: t for t in msgs.list()},
            # build_workitem_index now also defaults to excluding archived items
            # (MOS-228 T3); without this the store's own include_archived=all_items
            # fetch above would be silently re-filtered back out for `--all`.
            include_archived=all_items,
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

    @item_app.command("archive")
    def archive(item_id: str,
                force: bool = typer.Option(False, "--force", "-f",
                    help="Archive even though a live task still references this item.")):
        """Soft-delete a work item: hidden from `item list` unless `--all`.

        Refused when a live task (present in state.tasks — `mship close` removes a
        task from state entirely, so presence there is by definition not yet closed)
        is attached to this item via EITHER direction of the task<->item link:
        reverse (task.work_item_id == item_id) or forward (the item's own
        task_slugs). The two links are normally kept in sync (WorkItemStore.add_task
        sets both), but a stale/missing reverse link must not let a forward-linked
        live task slip through the guard. Pass --force to archive anyway (e.g. an
        abandoned/orphaned task)."""
        items, _, state_manager, _, _ = _ctx()
        output = Output()
        if not force:
            state = state_manager.load()
            reverse = {slug for slug, t in state.tasks.items() if t.work_item_id == item_id}
            wi = items.get(item_id)
            forward = {slug for slug in (wi.task_slugs if wi is not None else [])
                      if slug in state.tasks}
            blocking = sorted(reverse | forward)
            if blocking:
                output.error(
                    f"Work item {item_id} still has live task(s): "
                    f"{', '.join(blocking)}. Run `mship item archive {item_id} "
                    f"--force` to archive anyway."
                )
                raise typer.Exit(1)
        try:
            items.archive(item_id, now=datetime.now(timezone.utc))
        except KeyError:
            output.error(f"no work item {item_id!r}")
            raise typer.Exit(1)
        typer.echo(f"archived {item_id}")

    @item_app.command("unarchive")
    def unarchive(item_id: str):
        """Reverse of `item archive`: restore an item to the default `item list`."""
        items, _, _, _, _ = _ctx()
        output = Output()
        try:
            items.unarchive(item_id, now=datetime.now(timezone.utc))
        except KeyError:
            output.error(f"no work item {item_id!r}")
            raise typer.Exit(1)
        typer.echo(f"unarchived {item_id}")

    def _build_run_deps(holder: str, now) -> RunDeps:
        """Wire the runner's injectable edges to this workspace's real stores.

        The impure edges: run_state → a git-backed RunStateRepo on the workspace
        origin (workdir under the state dir); build_base_prompt → spec-first prompt;
        branch_state → the item's task branch/journal facts; mark_blocked → set the
        item's task(s) blocked_reason (the existing, derived block mechanism);
        push_branch → push the item's task branch to origin so a bail survives for a
        later resume. The ``blocked`` set (item-ids with a blocked task) is excluded
        by the selector so a bailed item isn't re-picked every tick (FIX#1). The pure
        ``claimed`` snapshot is empty — ``try_claim`` is the authoritative gate
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

        item_list = items.list()
        snapshot = state_manager.load()
        blocked = {
            it.id for it in item_list
            if any(snapshot.tasks[s].blocked_reason
                   for s in it.task_slugs if s in snapshot.tasks)
        }

        def mark_blocked(item, reason):
            stamp = now()

            def _apply(s):
                for slug in item.task_slugs:
                    if slug in s.tasks:
                        s.tasks[slug].blocked_reason = reason
                        s.tasks[slug].blocked_at = stamp
            state_manager.mutate(_apply)

        def push_branch(item):
            """Push the item's task branch to origin from each existing worktree, so
            a later (possibly fresh-clone) run can resume it. Best-effort: no commits,
            no creds, or offline must not strand the bail (checkpoint_bail guards it)."""
            task = next((snapshot.tasks[s] for s in item.task_slugs
                         if s in snapshot.tasks), None)
            if task is None:
                return
            for wt in task.worktrees.values():
                wt = Path(wt)
                if not wt.is_dir():
                    continue
                subprocess.run(
                    ["git", "push", "origin", f"HEAD:{task.branch}"],
                    cwd=str(wt), capture_output=True, text=True,
                )

        return RunDeps(
            items=item_list,
            spec_approved=spec_approved,
            claimed=set(),
            blocked=blocked,
            # Child state so the selector resolves each item's DERIVED phase — an item
            # whose override was cleared (Reopen) but whose spec is approved still
            # derives to `ready` and stays selectable (Finding 3).
            specs_by_id=specs_by_id,
            tasks_by_slug=dict(snapshot.tasks),
            run_state=run_state,
            build_base_prompt=lambda it: _base_prompt_for(
                it, specs_by_id.get(it.spec_id) if it.spec_id else None),
            branch_state=lambda it: _branch_state_for(
                it, state_manager.load(), log_mgr, config),
            mark_blocked=mark_blocked,
            push_branch=push_branch,
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

    @item_app.command("heartbeat")
    def heartbeat(item_id: str):
        """Advance a claimed item's run-heartbeat so a long unattended run isn't
        reclaimed mid-flight (the host calls this periodically during a run).

        Authoritative across processes: run-next claimed under a different process's
        holder token, so we read the claim's RECORDED holder off the ref and refresh
        as it (a fresh token would no-op). No live claim ⇒ nothing to do. FIX#3b."""
        items, _, _, _, _ = _ctx()
        item = items.get(item_id)
        if item is None:
            typer.echo(f"no work item {item_id!r}", err=True)
            raise typer.Exit(1)
        deps = _build_run_deps(holder=_run_holder(), now=_utcnow)
        claim = deps.run_state.read_claim(item_id)
        beat = claim is not None
        if beat:
            deps.run_state.refresh(item_id, claim.holder, _utcnow())
        output = Output()
        if output.json_mode:
            output.json({"item_id": item_id, "heartbeat": beat})
        elif beat:
            typer.echo(f"heartbeat {item_id} (holder {claim.holder})")
        else:
            typer.echo(f"no live claim for {item_id}")

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

"""`mship capture` — capture the running UI for an agent to inspect."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output
from mship.cli.remote_flags import RemoteFlagCommand
from mship.core import capture as _cap
from mship.core.dispatch import resolve_repo
from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
    resolve_task,
)


def _attach_evidence(
    *,
    artifacts,
    evidence: str,
    container,
    output,
    worktree: Path,
    platform: str | None,
    provenance: str | None = None,
) -> None:
    """Promote captured artifacts into acceptance-criterion evidence.

    Fail-open: the capture already succeeded, so a storage or spec failure warns
    and returns. It must never turn a good capture into a bad exit code — the
    same posture as `spawn`'s --closes issue linking.
    """
    from mship.core.evidence_attach import parse_evidence_target, provenance_note
    from mship.core.evidence_store import resolve_evidence_mode, store_artifact
    from mship.core.spec import AcceptanceEvidence
    from mship.core.spec_store import SpecStore

    try:
        target = parse_evidence_target(evidence)
        workspace_root = Path(container.config_path()).parent
        mode = resolve_evidence_mode(container.config())
        store = SpecStore(workspace_root / "specs")
        # Load, mutate, and save under the same per-spec lock. Capturing an
        # artifact can take long enough for an apply to replace the draft; saving
        # a pre-lock snapshot after that replacement would otherwise restore the
        # old body and review state.
        with store.locked(target.spec_id) as artifact:
            if artifact is None:
                output.warning(f"could not attach evidence: no spec {target.spec_id!r}")
                return
            spec = artifact.spec
            crit = next(
                (c for c in spec.acceptance_criteria if c.id == target.criterion_id),
                None,
            )
            if crit is None:
                output.warning(
                    f"could not attach evidence: {target.spec_id!r} has no criterion "
                    f"{target.criterion_id!r}"
                )
                return
            note_where = provenance or provenance_note(worktree, container.shell())
            for a in artifacts:
                ref = store_artifact(workspace_root, target.spec_id, a.path, mode=mode)
                crit.evidence.append(
                    AcceptanceEvidence(
                        kind="artifact",
                        ref=ref,
                        note=f"{a.kind} · {platform or 'default'} · {note_where}",
                    )
                )
            store.save_while_locked(spec, artifact)
        # human_mode only: `success` writes to STDOUT, which in JSON mode already
        # carries the capture payload — a confirmation line there would corrupt
        # it for `| jq`. Warnings above are safe (they go to stderr in JSON mode).
        if output.human_mode:
            output.success(
                f"attached {len(artifacts)} artifact(s) to "
                f"{target.spec_id}:{target.criterion_id}"
            )
    except Exception as e:
        output.warning(f"could not attach evidence: {e}")


def register(app: typer.Typer, get_container):
    @app.command(cls=RemoteFlagCommand, rich_help_panel="Runtime")
    def capture(
        task: Optional[str] = typer.Option(
            None, "--task", help="Target task slug (defaults to cwd-resolved)."
        ),
        repo: Optional[str] = typer.Option(
            None,
            "--repo",
            help="Which repo to capture (required for an ad-hoc capture when the workspace has >1 repo).",
        ),
        platform: Optional[str] = typer.Option(
            None,
            "--platform",
            help="Platform to capture (required when the repo exposes more than one).",
        ),
        run_id: Optional[str] = typer.Option(
            None,
            "--run-id",
            help="Capture one acknowledged profile run; never selects a device directly.",
        ),
        profile: Optional[str] = typer.Option(
            None, "--profile", help="Filter recorded runs by profile."
        ),
        host: Optional[str] = typer.Option(
            None, "--host", help="Filter recorded runs by their original host."
        ),
        target: Optional[str] = typer.Option(
            None, "--target", help="Filter recorded runs by an attested friendly alias."
        ),
        kind: str = typer.Option(
            "all", "--kind", help="Artifact kind: image | layout | all."
        ),
        out: Optional[Path] = typer.Option(
            None,
            "--out",
            help="Output directory (default: .mothership/captures/<task-or-_adhoc>/<ts>-<platform>/).",
        ),
        evidence: Optional[str] = typer.Option(
            None,
            "--evidence",
            metavar="SPEC:AC",
            help="Attach the captured artifact(s) to an acceptance criterion as "
            "kind=artifact evidence, e.g. --evidence my-spec:ac3. Without "
            "this flag the capture stays an ephemeral develop-verify-iterate "
            "artifact and nothing is stored or attached.",
        ),
        remote: Optional[str] = typer.Option(
            None,
            "--remote",
            help="Execute the capture on a mapped run-host role instead of "
            "locally. Bare --remote auto-resolves the role (this repo's "
            "declared run_host, else the sole configured run_hosts "
            "entry); --remote=<role> picks one explicitly. Requires an "
            "active task (the remote materializes the task's branch — "
            "there's no ad-hoc remote capture). Without this flag, "
            "behavior is unchanged (local).",
        ),
    ):
        """Capture the running UI (screenshot + layout) into files to read.

        Task-aware but not task-required: with an active task, captures run in the
        task's worktree and are filed under the task. Without one, capture runs an
        ad-hoc capture against a repo's main checkout — capture observes a running
        app, not worktree source, so it shouldn't require a task.
        """
        output = Output()
        container = get_container()
        state = container.state_manager().load()
        config = container.config()

        try:
            kinds = _cap.resolve_kinds(kind)
        except _cap.CaptureError as e:
            output.error(str(e))
            raise typer.Exit(code=2)

        # Resolve a task if one is anchored. No active task -> ad-hoc capture
        # against a repo's main checkout. Ambiguous/unknown still error: the user
        # clearly has tasks and should disambiguate rather than silently fall back.
        try:
            t, source = resolve_task(
                state,
                cli_task=task,
                env_task=os.environ.get("MSHIP_TASK"),
                cwd=Path.cwd(),
            )
        except NoActiveTaskError:
            t, source = None, None
        except (AmbiguousTaskError, UnknownTaskError) as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        if t is not None:
            output.breadcrumb(f"→ task: {t.slug}  (resolved via {source.value})")
            try:
                resolved_repo = resolve_repo(t, repo)
            except ValueError as e:
                output.error(str(e))
                raise typer.Exit(code=1)
            worktree = Path(t.worktrees[resolved_repo])
            out_bucket = t.slug
        else:
            try:
                resolved_repo = _cap.resolve_adhoc_repo(list(config.repos), repo)
            except _cap.CaptureError as e:
                output.error(str(e))
                raise typer.Exit(code=1)
            # repo.path is resolved to an absolute main-checkout path at load time.
            worktree = Path(config.repos[resolved_repo].path)
            out_bucket = "_adhoc"

        repo_cfg = config.repos[resolved_repo]
        session_observation = any(
            value is not None for value in (run_id, profile, host, target)
        ) or (t is not None and bool(repo_cfg.run_profiles))
        if not session_observation and t is not None:
            with container.state_manager().workspace_store.read() as transaction:
                candidates = transaction.app_runs.list_candidates(
                    transaction.connection, task_slug=t.slug, repo=resolved_repo
                )
            session_observation = bool(candidates)
        selected = None
        resolved_platform = platform
        if session_observation:
            from mship.cli.run_target import choose_run
            from mship.core.run_host import RunHostError
            from mship.core.session_capture import (
                SessionCaptureError,
                select_session_capture,
            )

            if t is None:
                output.error("recorded session capture requires an active task.")
                raise typer.Exit(code=1)
            try:
                selected = select_session_capture(
                    store=container.state_manager().workspace_store,
                    config=config,
                    task=t,
                    repo_name=resolved_repo,
                    run_id=run_id,
                    platform=platform,
                    profile_name=profile,
                    host_name=host,
                    target_alias=target,
                    choose=lambda candidates: choose_run(
                        candidates,
                        interactive=output.is_tty and output.human_mode,
                        input_fn=input,
                        output=output,
                    ),
                )
                if remote not in {None, ""} and remote not in selected.host.roles:
                    raise SessionCaptureError(
                        "specified remote role does not match the recorded run host"
                    )
                resolved_platform = selected.platform
            except (RunHostError, ValueError) as error:
                output.error(str(error))
                raise typer.Exit(code=1)
        else:
            platforms = repo_cfg.capture.platforms if repo_cfg.capture else []
            if resolved_platform is None:
                if len(platforms) == 1:
                    resolved_platform = platforms[0]
                elif len(platforms) > 1:
                    output.error(
                        f"--platform is required for repo {resolved_repo!r}; "
                        f"choose one of: {', '.join(platforms)}."
                    )
                    raise typer.Exit(code=2)
            elif platforms and resolved_platform not in platforms:
                output.error(
                    f"unknown platform {resolved_platform!r} for repo {resolved_repo!r}; "
                    f"choose one of: {', '.join(platforms)}."
                )
                raise typer.Exit(code=2)

        actual = repo_cfg.tasks.get("capture", "capture")
        if out is not None:
            out_dir = out
        else:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            workspace_root = Path(container.config_path()).parent
            label = resolved_platform or "default"
            out_dir = (
                workspace_root
                / ".mothership"
                / "captures"
                / out_bucket
                / f"{ts}-{label}"
            )

        if remote is not None or session_observation:
            from mship.core.remote_client import (
                RemoteExecError,
                exec_session_capture,
            )
            from mship.core.run_host import RunHostError, RunHostResolver
            from mship.core.evidence_attach import remote_provenance_note

            if session_observation:
                assert selected is not None
                try:
                    code = exec_session_capture(
                        operation=selected.operation,
                        kinds=kinds,
                        platform=resolved_platform or "",
                        host=selected.host,
                        resolver=RunHostResolver(),
                        captures_dir_for=out_dir,
                        print_fn=output.progress,
                    )
                except (RemoteExecError, RunHostError, ValueError) as e:
                    output.error(str(e))
                    raise typer.Exit(code=1)
                remote_note = remote_provenance_note("session observation")
            else:
                from mship.cli.exec import _run_remote

                # Remote execution always materializes the task's branch on the
                # remote — there's no ad-hoc remote capture (an ad-hoc capture
                # has no task/branch for the remote to check out).
                if t is None:
                    output.error(
                        "--remote requires an active task: the remote "
                        "materializes the task's branch, so there's no ad-hoc "
                        "remote capture. Pass --task, or run capture from an "
                        "active task's worktree."
                    )
                    raise typer.Exit(code=1)

                remote_note: str | None = None

                def record_preparation(source_preparation: str) -> None:
                    nonlocal remote_note
                    remote_note = remote_provenance_note(source_preparation)

                code = _run_remote(
                    verb="capture",
                    remote_role=remote or "",
                    task_obj=t,
                    target_repos=[resolved_repo],
                    config=config,
                    container=container,
                    output=output,
                    platform=resolved_platform,
                    kind=kind,
                    captures_dir_for=out_dir,
                    on_prepared=record_preparation,
                )

            # On success, emit the SAME confirmation a local capture does
            # (respecting --json), pointing at the local landing path where
            # the artifacts were extracted — a remote capture should be
            # indistinguishable from a local one to the caller. Re-discover the
            # extracted files (exec_remote returns only the exit code).
            if code == 0:
                landed = _cap.discover_artifacts(out_dir, kinds)
                if not landed:
                    # Defense-in-depth: a stale/older remote may return exit 0
                    # with no artifact block. Local capture treats "success
                    # with no recognized artifact" as a hard error — enforce
                    # the same here INDEPENDENTLY of the server-side check
                    # (don't trust a bare exit 0), with the same message/exit.
                    output.error(
                        f"capture target produced no recognized artifact in "
                        f"{out_dir} for kinds {kinds}."
                    )
                    raise typer.Exit(code=1)
                if output.human_mode:
                    for a in landed:
                        output.success(f"captured {a.kind} → {a.path}")
                else:
                    output.json(
                        {
                            "platform": resolved_platform,
                            "repo": resolved_repo,
                            "artifacts": [
                                {"kind": a.kind, "path": str(a.path)} for a in landed
                            ],
                            "resolved_task": t.slug if t is not None else None,
                            "resolution_source": source.value
                            if source is not None
                            else None,
                        }
                    )
                if evidence:
                    _attach_evidence(
                        artifacts=landed,
                        evidence=evidence,
                        container=container,
                        output=output,
                        worktree=worktree,
                        platform=resolved_platform,
                        provenance=remote_note
                        or remote_provenance_note(
                            "source preparation was not recorded"
                        ),
                    )
            raise typer.Exit(code=code)

        try:
            artifacts = _cap.run_capture(
                shell=container.shell(),
                worktree=worktree,
                actual_task_name=actual,
                env_runner=repo_cfg.env_runner,
                platform=resolved_platform,
                kinds=kinds,
                out_dir=out_dir,
            )
        except _cap.CaptureError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        if output.human_mode:
            for a in artifacts:
                output.success(f"captured {a.kind} → {a.path}")
        else:
            output.json(
                {
                    "platform": resolved_platform,
                    "repo": resolved_repo,
                    "artifacts": [
                        {"kind": a.kind, "path": str(a.path)} for a in artifacts
                    ],
                    "resolved_task": t.slug if t is not None else None,
                    "resolution_source": source.value if source is not None else None,
                }
            )

        if evidence:
            _attach_evidence(
                artifacts=artifacts,
                evidence=evidence,
                container=container,
                output=output,
                worktree=worktree,
                platform=resolved_platform,
            )

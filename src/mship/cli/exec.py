import os
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock
from typing import Optional

import typer

from mship.cli.output import Output
from mship.cli.remote_flags import RemoteFlagCommand


def _relpath(path_str: str) -> str:
    """Shorten for display: relative to cwd if possible, else absolute."""
    from pathlib import Path

    try:
        return str(Path(path_str).relative_to(Path.cwd()))
    except ValueError:
        return path_str


def _file_nonempty(path_str: str) -> bool:
    """True if the path exists and has non-zero size. False on OSError."""
    from pathlib import Path

    try:
        return Path(path_str).stat().st_size > 0
    except OSError:
        return False


def _resolve_repos(
    config,
    task_affected: list[str],
    repos_filter: str | None,
    tag_filter: list[str] | None,
) -> list[str]:
    """Resolve target repos from --repos and --tag filters."""
    candidates = None

    if repos_filter:
        candidates = set(repos_filter.split(","))
        for name in candidates:
            if name not in config.repos:
                raise ValueError(
                    f"Unknown repo '{name}'. Available: {', '.join(sorted(config.repos.keys()))}."
                )

    if tag_filter:
        tagged = set()
        for name, repo in config.repos.items():
            if any(t in repo.tags for t in tag_filter):
                tagged.add(name)
        if candidates is not None:
            candidates = candidates & tagged
        else:
            candidates = tagged

    if candidates is not None:
        return list(candidates)
    return task_affected


def _run_remote(
    *,
    verb: str,
    remote_role: str,
    task_obj,
    target_repos: list[str],
    config,
    container,
    output: Output,
    platform: str | None = None,
    kind: str = "all",
    captures_dir_for: Path | None = None,
    on_prepared: Callable[[str], None] | None = None,
) -> int:
    """Shared `--remote` dispatch for `run`/`build`/`capture`: resolve the
    run-host role to a connection, POST to the remote's `/exec/{verb}`, and
    return the remote task's exit code (mirrored by the caller as `raise
    typer.Exit(code)`).

    `task_obj` is the caller's already-resolved `Task` (`None` when nothing
    resolved). The OBJECT, not its slug: the preflight below is mandatory, and
    re-reading state to look the slug up again would open a window in which the
    task is gone from the second read — which skipped the whole check and
    dispatched anyway. There is nothing to re-read: `resolve_task` returns a task
    out of the state the caller already loaded.

    `remote_role` is the raw `--remote` CLI value: `""` (bare flag) means
    auto-resolve the role (repo's declared `run_host`, else the sole
    configured `run_hosts` entry); a non-empty string is an explicit
    `--remote=<role>`.

    Remote execution always operates on a task's branch — the remote
    materializes `.worktrees/<task>/<repo>`, either from origin or from a
    scratch ref this command pushes it — so there's no "ad-hoc" remote run.
    Local `run`/`build` gracefully fall back to "the whole workspace" when no
    task is active, but that fallback has no branch for the remote to check out,
    so `--remote` without a resolvable task is a clean, actionable CLI error
    rather than a confusing remote-side failure.

    A caller may supply capture-specific `platform`, `kind`, and
    `captures_dir_for` arguments, plus `on_prepared` to receive a conservative
    description of the source preflight completed before dispatch. This keeps
    capture on the same preparation path as run/build without making it infer
    source provenance from the local worktree after a remote capture.

    A `RunHostError` (unknown/ambiguous/unmapped role) or `RemoteExecError`
    (remote unreachable) surfaces as `output.error(...)` + `typer.Exit(1)` —
    never a bare traceback.
    """
    from mship.core import run_transfer
    from mship.core.remote_client import RemoteExecError, exec_remote
    from mship.core.remote_dispatch import RemoteDispatchError, prepare_remote_source
    from mship.core.run_host import (
        RunHostError,
        RunHostResolver,
        RunHostStore,
        resolve_run_host,
    )

    if task_obj is None:
        output.error(
            "--remote requires a resolvable task: the remote materializes "
            "the task's branch, so there's no ad-hoc remote run. Pass "
            "--task, or run from an active task's worktree."
        )
        raise typer.Exit(code=1)

    role = remote_role or None
    repo_for_host = config.repos[target_repos[0]] if len(target_repos) == 1 else None

    store = RunHostStore(container.state_dir())
    try:
        host = resolve_run_host(role, repo=repo_for_host, config=config, store=store)
    except RunHostError as e:
        output.error(str(e))
        raise typer.Exit(code=1)
    resolver = RunHostResolver()

    def record_transfer(git_repo: str, ref: str, sha: str) -> None:
        run_transfer.record_run_ref_receipt(
            container.state_dir(),
            task=task_obj,
            host=host,
            repo=git_repo,
            ref=ref,
            sha=sha,
        )

    try:
        prepared = prepare_remote_source(
            task_obj=task_obj,
            target_repos=target_repos,
            config=config,
            shell=container.shell(),
            host=host,
            resolver=resolver,
            output=output,
            on_prepared=on_prepared,
            on_transfer=record_transfer,
        )
    except RemoteDispatchError as e:
        output.error(str(e))
        raise typer.Exit(code=1)
    try:
        recorded_repos: set[str] = set()
        for repo_name in target_repos:
            git_repo = config.repos[repo_name].git_root or repo_name
            source_revision = prepared.source_revisions.get(repo_name)
            if not isinstance(source_revision, str):
                raise run_transfer.RunTransferError(
                    "could not record an uncertified remote worktree revision"
                )
            if git_repo in recorded_repos:
                continue
            recorded_repos.add(git_repo)
            run_transfer.record_remote_worktree_receipt(
                container.state_dir(),
                task=task_obj,
                host=host,
                repo=git_repo,
                sha=source_revision,
            )
    except run_transfer.RunTransferError:
        output.error("could not record exact remote task-worktree cleanup")
        raise typer.Exit(code=1)
    try:
        return exec_remote(
            verb=verb,
            host=host,
            resolver=resolver,
            task=task_obj.slug,
            repos=target_repos,
            platform=platform,
            kind=kind,
            captures_dir_for=captures_dir_for,
            run_ref_repos=list(prepared.run_ref_repos),
            print_fn=output.progress,
        )
    except (RemoteExecError, RunHostError) as e:
        output.error(str(e))
        raise typer.Exit(code=1)


def _run_profile(
    *,
    task_obj,
    target_repos: list[str],
    profile_name: str | None,
    host_name: str | None,
    target_alias: str | None,
    remote_role: str | None,
    container,
    config,
    output: Output,
) -> int:
    """Preflight every profile context, then launch them in dependency order."""
    from mship.cli.run_target import choose_profile, choose_target
    from mship.core.run_host import RunHostStore
    from mship.core.run_target.models import (
        AppRun,
        SelectedTarget,
        TargetSelectionError,
    )
    from mship.core.run_target.preferences import TargetPreferenceStore
    from mship.core.run_target.service import RemoteBackendExecutor, resolve_launch

    if task_obj is None:
        output.error("profile runs require a task-bound repository")
        return 1

    def stream_event(event) -> None:
        if event.kind in {"stdout", "stderr"} and event.data:
            output.progress(event.data.decode("utf-8", "replace"))

    executor = RemoteBackendExecutor(
        task_obj=task_obj,
        config=config,
        shell=container.shell(),
        output=output,
        store=container.state_manager().workspace_store,
        event_sink=stream_event,
    )
    registry = RunHostStore(container.state_dir())
    preferences = TargetPreferenceStore(container.state_dir())
    planned: dict[str, tuple[SelectedTarget, str]] = {}
    unprofiled: list[str] = []

    try:
        for repo_name in target_repos:
            repo = config.repos[repo_name]
            selected_profile = profile_name or repo.default_run_profile
            if selected_profile is None:
                if repo.run_profiles:
                    selected_profile = choose_profile(
                        tuple(sorted(repo.run_profiles)),
                        interactive=output.is_tty and output.human_mode,
                        input_fn=input,
                        output=output,
                    )
                elif (
                    profile_name is not None
                    or host_name is not None
                    or target_alias is not None
                ):
                    raise TargetSelectionError(
                        "profile_missing",
                        f"repository {repo_name!r} has no configured run profile",
                    )
                else:
                    unprofiled.append(repo_name)
                    continue
            if selected_profile not in repo.run_profiles:
                raise TargetSelectionError(
                    "profile_missing",
                    f"repository {repo_name!r} does not configure profile {selected_profile!r}",
                )
            selected = resolve_launch(
                config=config,
                task=task_obj,
                repo_name=repo_name,
                profile_name=selected_profile,
                host_name=host_name,
                remote_role=remote_role or None,
                target_alias=target_alias,
                registry=registry,
                preferences=preferences,
                execute=executor,
                choose=lambda candidates, repo=repo, name=selected_profile: (
                    choose_target(
                        candidates,
                        profile_name=name,
                        backend_name=repo.run_profiles[name].backend,
                        interactive=output.is_tty and output.human_mode,
                        input_fn=input,
                        output=output,
                    )
                ),
            )
            planned[repo_name] = (selected, selected_profile)
    except TargetSelectionError as error:
        output.error(str(error))
        return 1

    if unprofiled:
        build = container.executor().execute(
            "build", repos=unprofiled, task_slug=task_obj.slug
        )
        if not build.success:
            for result in build.results:
                if not result.success:
                    output.error(f"{result.repo}: build failed before profile launch")
            return 1

    announced: set[str] = set()
    announce_lock = Lock()

    def announce_ready(repo_name: str, run: AppRun) -> None:
        with announce_lock:
            if repo_name in announced:
                return
            announced.add(repo_name)
        message = f"{repo_name}: run {run.id} is ready"
        if output.human_mode:
            output.success(message)
        else:
            output.progress(message)

    def start_selected(
        selected: SelectedTarget, repo_name: str, selected_profile: str
    ) -> Callable[[Callable[[AppRun], None], Event], AppRun]:
        def launch(on_ready: Callable[[AppRun], None], cancel_event: Event) -> AppRun:
            def ready(run: AppRun) -> None:
                announce_ready(repo_name, run)
                on_ready(run)

            return executor.launch_selected(
                selected,
                repo_name=repo_name,
                profile_name=selected_profile,
                on_ready=ready,
                cancel_event=cancel_event,
            )

        return launch

    try:
        finals = container.executor().launch_profiled(
            {
                repo_name: start_selected(selected, repo_name, selected_profile)
                for repo_name, (selected, selected_profile) in planned.items()
            },
            cancel=executor.cancel_run,
        )
    except (TargetSelectionError, RuntimeError) as error:
        output.error(str(error))
        return 1

    run_statuses = {
        repo_name: {"run_id": final.id, "status": final.status}
        for repo_name, final in finals.items()
    }
    failed = False
    for repo_name in target_repos:
        final = finals.get(repo_name)
        if final is None or final.status in {"active", "stopped"}:
            if final is not None and final.status == "active":
                announce_ready(repo_name, final)
            continue
        failed = True
        if final.status == "unknown":
            output.error(
                f"{repo_name}: profile run outcome is unknown; establish a new run before observing"
            )
        elif repo_name in announced:
            output.error(f"{repo_name}: profile run failed after ready acknowledgement")
        else:
            output.error(
                f"{repo_name}: profile run failed before a trusted ready acknowledgement"
            )
    if output.json_mode:
        output.json({"command": "run", "runs": run_statuses})
    if failed:
        return 1

    return 0


def _update_profile_run(
    *,
    task_obj,
    target_repos: list[str],
    run_id: str | None,
    remote_role: str | None,
    container,
    config,
    output: Output,
) -> int:
    """Transfer one new snapshot to the exact recorded Flutter owner, then reload."""
    from mship.core import run_transfer
    from mship.core.remote_client import RemoteExecError, source_update_remote
    from mship.core.remote_dispatch import (
        RemoteDispatchError,
        prepare_remote_source,
        snapshot_remote_source,
    )
    from mship.core.run_host import RunHostError, RunHostResolver
    from mship.core.run_target.models import profile_revision
    from mship.core.session_capture import SessionCaptureError, select_session_capture
    from mship.core.session_source import SourceUpdateError, update_and_reload

    if task_obj is None or len(target_repos) != 1:
        output.error("update-and-hot-reload requires one task-bound repository")
        return 1
    try:
        selected = select_session_capture(
            store=container.state_manager().workspace_store,
            config=config,
            task=task_obj,
            repo_name=target_repos[0],
            run_id=run_id,
            platform=None,
            operation_name="reload",
        )
        if remote_role not in {None, ""} and remote_role not in selected.host.roles:
            raise SessionCaptureError(
                "specified remote role does not match the recorded run host"
            )
        repo = config.repos[selected.run.repo]
        profile = repo.run_profiles[selected.run.profile]
        backend = repo.run_backends[selected.run.backend]
        if backend.session_owner != "flutter":
            raise SessionCaptureError("recorded run is not a Flutter session")
        if backend.operations.get("run") in repo.task_outputs:
            raise SessionCaptureError(
                "source updates cannot change a result-producing run"
            )
        snapshot = snapshot_remote_source(
            task_obj=task_obj,
            target_repos=[selected.run.repo],
            config=config,
            shell=container.shell(),
        )

        def record_transfer(git_repo: str, ref: str, sha: str) -> None:
            run_transfer.record_run_ref_receipt(
                container.state_dir(),
                task=task_obj,
                host=selected.host,
                repo=git_repo,
                ref=ref,
                sha=sha,
            )
            run_transfer.record_remote_worktree_receipt(
                container.state_dir(),
                task=task_obj,
                host=selected.host,
                repo=git_repo,
                sha=sha,
            )

        resolver = RunHostResolver()
        prepared = prepare_remote_source(
            task_obj=task_obj,
            target_repos=[selected.run.repo],
            config=config,
            shell=container.shell(),
            host=selected.host,
            resolver=resolver,
            output=output,
            snapshot=snapshot,
            on_transfer=record_transfer,
            run_ref_only=True,
        )
        revision = prepared.source_revisions.get(selected.run.repo)
        if not isinstance(revision, str) or revision != snapshot.source_revisions.get(
            selected.run.repo
        ):
            raise RemoteDispatchError(
                "could not certify source identity for recorded Flutter run"
            )
        updated = update_and_reload(
            selected.run,
            selected.operation,
            new_source_revision=revision,
            new_profile_revision=profile_revision(
                profile, backend, prepared_source_revision=revision
            ),
            store=container.state_manager().workspace_store,
            exchange=lambda request: source_update_remote(
                host=selected.host, resolver=resolver, request=request
            ),
        )
    except (
        RemoteDispatchError,
        RemoteExecError,
        RunHostError,
        SessionCaptureError,
        SourceUpdateError,
        ValueError,
    ):
        output.error("source update could not be completed")
        return 1
    if updated.status != "active":
        output.error("source update outcome is unknown; do not retry automatically")
        return 1
    output.success("updated recorded Flutter run")
    return 0


def register(app: typer.Typer, get_container):
    @app.command(name="test", rich_help_panel="Workflow")
    def test_cmd(
        run_all: bool = typer.Option(
            False, "--all", help="Run all repos even on failure"
        ),
        repos: Optional[str] = typer.Option(
            None, "--repos", help="Comma-separated repo names to filter"
        ),
        tag: Optional[list[str]] = typer.Option(
            None, "--tag", help="Filter repos by tag"
        ),
        no_diff: bool = typer.Option(
            False, "--no-diff", help="Skip cross-run diff output"
        ),
        task: Optional[str] = typer.Option(
            None,
            "--task",
            help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var.",
        ),
    ):
        """Run tests across affected repos; show diff vs. previous iteration."""
        from datetime import datetime, timezone
        from mship.cli._resolve import resolve_for_command
        from mship.core.test_history import (
            write_run,
            read_run,
            latest_iteration,
            compute_diff,
            prune,
        )

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        resolved = resolve_for_command("exec", state, task, output)
        t = resolved.task

        if t.active_repo and t.active_repo in t.passive_repos:
            output.error(
                f"Cannot run tests: active_repo '{t.active_repo}' is passive. "
                f"Switch to an affected repo first, or close & respawn with "
                f"`--repos {t.active_repo},...` to make it editable."
            )
            raise typer.Exit(code=1)

        from pathlib import Path as _P
        from mship.cli._cwd_check import format_cwd_warning

        if t.active_repo is not None and t.active_repo in t.worktrees:
            warn = format_cwd_warning(_P.cwd(), _P(t.worktrees[t.active_repo]))
            if warn is not None:
                output.print(f"[yellow]{warn}[/yellow]")

        config = container.config()

        try:
            target_repos = _resolve_repos(config, t.affected_repos, repos, tag)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        state_dir = container.state_dir()
        prev_iter = latest_iteration(state_dir, t.slug)
        prev_run = read_run(state_dir, t.slug, prev_iter) if prev_iter else None
        pre_prev_run = (
            read_run(state_dir, t.slug, prev_iter - 1)
            if prev_iter and prev_iter > 1
            else None
        )

        started_at = datetime.now(timezone.utc)

        executor = container.executor()
        from mship.core.executor import TestTargetConflictError

        try:
            result = executor.execute(
                "test",
                repos=target_repos,
                run_all=run_all,
                task_slug=t.slug,
            )
        except TestTargetConflictError as exc:
            implicit = ", ".join(exc.implicit_repos) or "(none)"
            explicit = ", ".join(exc.explicit_repos) or "(none)"
            output.error(
                f"{implicit} share path {exc.cwd} with {explicit} but resolve "
                f"`task test` to different targets. {implicit} has no `test` task; "
                f"declare one in `tasks.test`, list `test` in `not_applicable`, "
                f"or pass `--repos {explicit}`."
            )
            raise typer.Exit(code=1)

        run_duration_ms = int(
            (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
        )

        # Build per-repo results for the iteration file
        per_repo: dict[str, dict] = {}
        streams: dict[str, tuple[str, str]] = {}
        for r in result.results:
            if r.skipped:
                status = "skip"
            elif r.success:
                status = "pass"
            else:
                status = "fail"
            stderr_tail = None
            if status == "fail":
                stderr = (r.shell_result.stderr or "").splitlines()
                stderr_tail = "\n".join(stderr[-40:]) if stderr else None
            entry = {
                "status": status,
                "duration_ms": r.duration_ms,
                "exit_code": r.shell_result.returncode,
                "stderr_tail": stderr_tail,
            }
            if r.shared_with:
                entry["shared_with"] = list(r.shared_with)
            per_repo[r.repo] = entry
            streams[r.repo] = (
                r.shell_result.stdout or "",
                r.shell_result.stderr or "",
            )

        new_iter = (prev_iter or 0) + 1
        write_run(
            state_dir,
            t.slug,
            iteration=new_iter,
            started_at=started_at,
            duration_ms=run_duration_ms,
            results=per_repo,
            streams=streams,
        )

        # Persist iteration on task (read-modify-write under the lock).
        def _record(task):
            task.test_iteration = new_iter
            # Agent-agnostic activity heartbeat: running tests is task work.
            task.last_activity_at = datetime.now(timezone.utc)

        state_mgr.mutate_task(t.slug, _record)

        prune(state_dir, t.slug, keep=20)

        # Summary for log entry — skipped repos count as not-a-failure.
        fail_count = sum(1 for v in per_repo.values() if v["status"] == "fail")
        total = len(per_repo)
        if fail_count == 0:
            test_state = "pass"
        elif fail_count == total:
            test_state = "fail"
        else:
            test_state = "mixed"
        pass_count = total - fail_count  # for the log line below
        # If a debug thread is open, attach parent=<latest hypothesis id> so
        # tree-compilation tools can fold this test run into the hypothesis
        # being evaluated. See #30.
        from mship.core.debug import current_debug_thread

        thread = current_debug_thread(container.log_manager(), t.slug)
        parent_id = None
        if thread:
            # Latest `hypothesis` entry in the thread (search from end).
            for e in reversed(thread):
                if e.action == "hypothesis":
                    parent_id = e.id
                    break

        container.log_manager().append(
            t.slug,
            f"iter {new_iter}: {pass_count}/{total} passing",
            iteration=new_iter,
            test_state=test_state,
            action="ran tests",
            parent=parent_id,
        )

        # Render
        current_run = {
            "iteration": new_iter,
            "started_at": started_at.isoformat().replace("+00:00", "Z"),
            "duration_ms": run_duration_ms,
            "repos": per_repo,
        }
        diff = None if no_diff else compute_diff(current_run, prev_run, pre_prev_run)

        if output.human_mode:
            output.print(
                f"[bold]Test run #{new_iter}[/bold]  ({run_duration_ms / 1000:.1f}s)"
            )
            # Collapse path-share groups (#127): a set of repos that shared a
            # physical run renders as one line listing every member.
            rendered: set[str] = set()
            for repo_name, info in per_repo.items():
                if repo_name in rendered:
                    continue
                status = info["status"]
                color = (
                    "green"
                    if status == "pass"
                    else "yellow"
                    if status == "skip"
                    else "red"
                )
                dur_s = info["duration_ms"] / 1000
                shared = info.get("shared_with") or []
                if shared:
                    label = ", ".join(sorted([repo_name, *shared]))
                    rendered.update([repo_name, *shared])
                else:
                    label = repo_name
                    rendered.add(repo_name)
                line = f"  {label}: [{color}]{status}[/{color}]  ({dur_s:.1f}s)"
                if diff and repo_name in diff["tags"]:
                    repo_tag = diff["tags"][repo_name]
                    if repo_tag in {"new failure", "regression", "fix"}:
                        line += f"  ← {repo_tag}"
                output.print(line)
                if status == "fail":
                    stderr_path = info.get("stderr_path")
                    stdout_path = info.get("stdout_path")
                    if stderr_path:
                        output.print(f"    stderr: {_relpath(stderr_path)}")
                    if stdout_path and _file_nonempty(stdout_path):
                        output.print(f"    stdout: {_relpath(stdout_path)}")
                    if info["stderr_tail"]:
                        output.print("    last 20 lines of stderr:")
                        for tline in info["stderr_tail"].splitlines()[-20:]:
                            output.print(f"      {tline}")
            if diff:
                prev_id = diff["previous_iteration"]
                new_fail = diff["summary"]["new_failures"]
                fixes = diff["summary"]["fixes"]
                parts = [f"{pass_count}/{total} repos passing"]
                if prev_id is not None and new_fail:
                    parts.append(
                        f"{len(new_fail)} new failure(s) since iter #{prev_id}"
                    )
                if fixes:
                    parts.append(f"{len(fixes)} fix(es)")
                output.print("")
                output.print("  " + ". ".join(parts) + ".")
        else:
            payload = dict(current_run)
            if diff is not None:
                payload["diff"] = diff
            payload["resolved_task"] = resolved.task.slug
            payload["resolution_source"] = resolved.source
            output.json(payload)

        if not result.success:
            raise typer.Exit(code=1)

    @app.command(name="run", cls=RemoteFlagCommand, rich_help_panel="Runtime")
    def run_cmd(
        repos: Optional[str] = typer.Option(
            None, "--repos", help="Comma-separated repo names to filter"
        ),
        tag: Optional[list[str]] = typer.Option(
            None, "--tag", help="Filter repos by tag"
        ),
        task: Optional[str] = typer.Option(
            None, "--task", help="Narrow to one task's affected repos"
        ),
        profile: Optional[str] = typer.Option(
            None, "--profile", help="Configured run profile for a target-aware launch."
        ),
        host: Optional[str] = typer.Option(
            None, "--host", help="Constrain a profile run to one configured host."
        ),
        target: Optional[str] = typer.Option(
            None,
            "--target",
            help="Constrain a profile run to a configured target alias.",
        ),
        run_id: Optional[str] = typer.Option(
            None, "--run-id", help="Recorded Flutter run for --update-and-hot-reload."
        ),
        update_and_hot_reload: bool = typer.Option(
            False,
            "--update-and-hot-reload",
            help="Explicitly update source and hot reload one recorded Flutter run.",
        ),
        remote: Optional[str] = typer.Option(
            None,
            "--remote",
            help="Execute on a mapped run-host role instead of locally. Bare "
            "--remote auto-resolves the role (the repo's declared "
            "run_host, else the sole configured run_hosts entry); "
            "--remote=<role> picks one explicitly. Without this flag, "
            "behavior is unchanged (local).",
        ),
    ):
        """Start services across repos in dependency order."""
        import os as _os
        import signal
        from pathlib import Path as _P
        from mship.core.task_resolver import (
            AmbiguousTaskError,
            NoActiveTaskError,
            UnknownTaskError,
            resolve_task,
        )

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        config = container.config()

        # Services are workspace-scoped, not task-scoped. If a task can be
        # resolved (via --task / MSHIP_TASK / cwd), fall back to its affected
        # repos; otherwise operate over every repo in the workspace. We use
        # resolve_task directly (not resolve_or_exit) so the "no anchor,
        # multiple tasks" case degrades gracefully to "all repos" instead of
        # hard-erroring. An explicit --task or env that points at an unknown
        # slug is still an error.
        # The resolved Task itself, not just its slug — `_run_remote` preflights
        # against it, and looking it up again is a read that can come back empty.
        task_obj = None
        fallback_repos: list[str]
        try:
            t, _ = resolve_task(
                state,
                cli_task=task,
                env_task=_os.environ.get("MSHIP_TASK"),
                cwd=_P.cwd(),
            )
            fallback_repos = t.affected_repos
            task_obj = t
        except UnknownTaskError as e:
            known = ", ".join(sorted(state.tasks.keys())) or "(none)"
            output.error(f"Unknown task: {e.slug}. Known: {known}.")
            raise typer.Exit(1)
        except NoActiveTaskError, AmbiguousTaskError:
            fallback_repos = list(config.repos.keys())

        try:
            target_repos = _resolve_repos(config, fallback_repos, repos, tag)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        if update_and_hot_reload:
            if (
                run_id is None
                or profile is not None
                or host is not None
                or target is not None
            ):
                output.error(
                    "--update-and-hot-reload requires exactly --run-id and no launch selectors"
                )
                raise typer.Exit(code=1)
            raise typer.Exit(
                code=_update_profile_run(
                    task_obj=task_obj,
                    target_repos=target_repos,
                    run_id=run_id,
                    remote_role=remote,
                    container=container,
                    config=config,
                    output=output,
                )
            )

        if run_id is not None:
            output.error("--run-id is only valid with --update-and-hot-reload")
            raise typer.Exit(code=1)

        if (
            profile is not None
            or host is not None
            or target is not None
            or any(config.repos[name].run_profiles for name in target_repos)
        ):
            raise typer.Exit(
                code=_run_profile(
                    task_obj=task_obj,
                    target_repos=target_repos,
                    profile_name=profile,
                    host_name=host,
                    target_alias=target,
                    remote_role=remote,
                    container=container,
                    config=config,
                    output=output,
                )
            )

        if remote is not None:
            code = _run_remote(
                verb="run",
                remote_role=remote,
                task_obj=task_obj,
                target_repos=target_repos,
                config=config,
                container=container,
                output=output,
            )
            raise typer.Exit(code=code)

        executor = container.executor()
        result = executor.execute("run", repos=target_repos)

        def _kill_group(proc, sig):
            """Send sig to the whole process group. Cross-platform."""
            try:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(proc.pid, sig)
            except ProcessLookupError, OSError:
                try:
                    proc.send_signal(sig)
                except Exception:
                    pass
            except Exception:
                pass

        if not result.success:
            for repo_result in result.results:
                if not repo_result.success:
                    output.error(f"{repo_result.repo}: failed to start")
            # Terminate any background processes that did start
            for proc in result.background_processes:
                _kill_group(proc, signal.SIGINT)
            raise typer.Exit(code=1)

        if not result.background_processes:
            output.success("All services started")
            return

        # Have background services — wait for them with signal forwarding
        output.success(
            f"Started {len(result.background_processes)} background service(s):"
        )
        for repo_result in result.results:
            if repo_result.background_pid is None and repo_result.healthcheck is None:
                continue
            pid_part = (
                f"(pid {repo_result.background_pid})"
                if repo_result.background_pid
                else ""
            )
            hc_part = (
                f"  {repo_result.healthcheck.message}"
                if repo_result.healthcheck
                else ""
            )
            icon = "[green]✓[/green]" if repo_result.success else "[red]✗[/red]"
            output.print(
                f"  {icon} {repo_result.repo} → task {repo_result.task_name}  {pid_part}{hc_part}"
            )
        output.print("")
        output.print("Press Ctrl-C to stop.")

        def _forward_sigint(signum, frame):
            for proc in result.background_processes:
                _kill_group(proc, signal.SIGINT)

        signal.signal(signal.SIGINT, _forward_sigint)

        try:
            for proc in result.background_processes:
                proc.wait()
                # Catch any surviving grandchildren in the process group
                _kill_group(proc, signal.SIGTERM)
            # Brief grace period, then SIGKILL stragglers
            import time

            time.sleep(0.5)
            for proc in result.background_processes:
                _kill_group(proc, signal.SIGKILL if os.name != "nt" else signal.SIGTERM)
        except KeyboardInterrupt:
            for proc in result.background_processes:
                _kill_group(proc, signal.SIGINT)
            for proc in result.background_processes:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    _kill_group(
                        proc, signal.SIGKILL if os.name != "nt" else signal.SIGTERM
                    )
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass

        output.print("All background services have exited")

    @app.command(name="build", cls=RemoteFlagCommand, rich_help_panel="Workflow")
    def build_cmd(
        run_all: bool = typer.Option(
            False, "--all", help="Build all repos even if one fails"
        ),
        repos: Optional[str] = typer.Option(
            None, "--repos", help="Comma-separated repo names to filter"
        ),
        tag: Optional[list[str]] = typer.Option(
            None, "--tag", help="Filter repos by tag"
        ),
        task: Optional[str] = typer.Option(
            None,
            "--task",
            help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var.",
        ),
        remote: Optional[str] = typer.Option(
            None,
            "--remote",
            help="Execute on a mapped run-host role instead of locally. Bare "
            "--remote auto-resolves the role (the repo's declared "
            "run_host, else the sole configured run_hosts entry); "
            "--remote=<role> picks one explicitly. Without this flag, "
            "behavior is unchanged (local).",
        ),
    ):
        """Build artifacts across repos in dependency order (runs `task build`)."""
        import os as _os
        from pathlib import Path as _P
        from mship.core.task_resolver import (
            AmbiguousTaskError,
            NoActiveTaskError,
            UnknownTaskError,
            resolve_task,
        )

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()
        config = container.config()

        # Scope repos to a task if one resolves (cwd / MSHIP_TASK / --task);
        # otherwise build the whole workspace. Mirrors `run`'s graceful
        # fallback — an explicit unknown --task is still an error.
        # The resolved Task itself, not just its slug — see `run` above.
        task_obj = None
        fallback_repos: list[str]
        try:
            t, _ = resolve_task(
                state,
                cli_task=task,
                env_task=_os.environ.get("MSHIP_TASK"),
                cwd=_P.cwd(),
            )
            fallback_repos = t.affected_repos
            task_obj = t
        except UnknownTaskError as e:
            known = ", ".join(sorted(state.tasks.keys())) or "(none)"
            output.error(f"Unknown task: {e.slug}. Known: {known}.")
            raise typer.Exit(1)
        except NoActiveTaskError, AmbiguousTaskError:
            fallback_repos = list(config.repos.keys())
        task_slug = task_obj.slug if task_obj is not None else None

        try:
            target_repos = _resolve_repos(config, fallback_repos, repos, tag)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        if remote is not None:
            code = _run_remote(
                verb="build",
                remote_role=remote,
                task_obj=task_obj,
                target_repos=target_repos,
                config=config,
                container=container,
                output=output,
            )
            raise typer.Exit(code=code)

        executor = container.executor()
        result = executor.execute(
            "build",
            repos=target_repos,
            run_all=run_all,
            task_slug=task_slug,
        )

        def _status(r) -> str:
            return "skip" if r.skipped else ("pass" if r.success else "fail")

        if output.human_mode:
            output.print("[bold]Build[/bold]")
            for r in sorted(result.results, key=lambda r: r.repo):
                st = _status(r)
                color = "green" if st == "pass" else "yellow" if st == "skip" else "red"
                output.print(
                    f"  {r.repo}: [{color}]{st}[/{color}]  ({r.duration_ms / 1000:.1f}s)"
                )
                if st == "fail":
                    for line in (r.shell_result.stderr or "").splitlines()[-20:]:
                        output.print(f"      {line}")
            if result.success:
                output.print("")
                output.success("Build succeeded")
        else:
            output.json(
                {
                    "command": "build",
                    "repos": {
                        r.repo: {
                            "status": _status(r),
                            "duration_ms": r.duration_ms,
                            "exit_code": r.shell_result.returncode,
                        }
                        for r in result.results
                    },
                    "success": result.success,
                    "resolved_task": task_slug,
                }
            )

        if not result.success:
            raise typer.Exit(code=1)

    @app.command(rich_help_panel="Runtime")
    def logs(
        service: Optional[str] = typer.Argument(
            None, help="Service name (omit with --all)"
        ),
        all_services: bool = typer.Option(
            False, "--all", help="Tail logs for every service"
        ),
        task: Optional[str] = typer.Option(
            None, "--task", help="Prefer this task's worktrees for cwd"
        ),
        repo: Optional[str] = typer.Option(
            None, "--repo", help="Constrain --run-id logs to one recorded repository."
        ),
        profile: Optional[str] = typer.Option(
            None, "--profile", help="Constrain recorded-run logs to one profile."
        ),
        host: Optional[str] = typer.Option(
            None, "--host", help="Constrain recorded-run logs to one configured host."
        ),
        target: Optional[str] = typer.Option(
            None,
            "--target",
            help="Constrain recorded-run logs to a friendly target alias.",
        ),
        run_id: Optional[str] = typer.Option(
            None, "--run-id", help="Tail logs from one acknowledged profile run."
        ),
    ):
        """Tail logs for a specific service."""
        import os as _os
        from mship.core.task_resolver import (
            AmbiguousTaskError,
            NoActiveTaskError,
            UnknownTaskError,
            resolve_task,
        )

        container = get_container()
        output = Output()
        config = container.config()

        if all_services and service is not None:
            output.error("Pass either <service> or --all, not both.")
            raise typer.Exit(code=1)
        if run_id is not None and all_services:
            output.error(
                "--run-id identifies one run and cannot be combined with --all"
            )
            raise typer.Exit(code=1)
        if run_id is None and not all_services and service is None:
            available = ", ".join(sorted(config.repos.keys()))
            output.error(
                f"Service name required, or pass --all. Available: {available}."
            )
            raise typer.Exit(code=1)
        if repo is not None and service is not None and repo != service:
            output.error("--repo and SERVICE must name the same repository")
            raise typer.Exit(code=1)

        from pathlib import Path

        state_mgr = container.state_manager()
        state = state_mgr.load()
        shell = container.shell()
        resolved_task = None
        try:
            resolved_task, _ = resolve_task(
                state,
                cli_task=task,
                env_task=_os.environ.get("MSHIP_TASK"),
                cwd=Path.cwd(),
            )
        except UnknownTaskError as error:
            known = ", ".join(sorted(state.tasks.keys())) or "(none)"
            output.error(f"Unknown task: {error.slug}. Known: {known}.")
            raise typer.Exit(1)
        except NoActiveTaskError, AmbiguousTaskError:
            resolved_task = None

        if run_id is not None:
            repo_name = (
                repo
                or service
                or (
                    resolved_task.affected_repos[0]
                    if resolved_task is not None
                    and len(resolved_task.affected_repos) == 1
                    else None
                )
            )
            if resolved_task is None or repo_name is None:
                output.error(
                    "--run-id logs require an active task and one matching repository"
                )
                raise typer.Exit(code=1)
            targets = [repo_name]
        else:
            targets = sorted(config.repos) if all_services else [service]
        for name in targets:
            if name not in config.repos:
                available = ", ".join(sorted(config.repos.keys()))
                output.error(
                    f"Unknown service '{name}'. Available services: {available}."
                )
                raise typer.Exit(code=1)
        from mship.core.run_target.models import TargetSelectionError
        from mship.core.session_capture import SessionCaptureError

        def log_selected_run(name: str) -> None:
            import json

            from mship.cli.run_target import choose_run
            from mship.core.run_target.models import BackendExecution
            from mship.core.run_target.service import RemoteBackendExecutor
            from mship.core.session_capture import select_session_capture

            assert resolved_task is not None
            selected = select_session_capture(
                store=state_mgr.workspace_store,
                config=config,
                task=resolved_task,
                repo_name=name,
                run_id=run_id,
                platform=None,
                operation_name="logs",
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
            request = json.loads(
                selected.operation.input_files["MSHIP_TARGET_REQUEST_FILE"]
            )
            remote_executor = RemoteBackendExecutor(
                task_obj=resolved_task,
                config=config,
                shell=shell,
                output=output,
                store=state_mgr.workspace_store,
                event_sink=lambda event: (
                    output.progress(event.data.decode("utf-8", "replace"))
                    if event.kind in {"stdout", "stderr"} and event.data
                    else None
                ),
            )
            result = remote_executor(
                selected.host,
                BackendExecution(
                    task=selected.run.task_slug,
                    repo=selected.run.repo,
                    profile=selected.run.profile,
                    backend=selected.run.backend,
                    logical_task=selected.operation.task_key,
                    operation="logs",
                    request=request,
                    run_id=selected.run.id,
                    preparation="observe",
                    max_stdout_bytes=None,
                    max_stderr_bytes=None,
                    timeout_seconds=None,
                ),
            )
            if result.error_code is not None:
                raise ValueError("recorded run logs are unavailable")

        for name in targets:
            repo_config = config.repos[name]
            use_recorded_run = resolved_task is not None and (
                run_id is not None or bool(repo_config.run_profiles)
            )
            if profile is not None or host is not None or target is not None:
                use_recorded_run = True
            if use_recorded_run:
                if all_services:
                    output.print(f"[bold]── {name} ──[/bold]")
                try:
                    log_selected_run(name)
                except (
                    SessionCaptureError,
                    TargetSelectionError,
                    ValueError,
                    KeyError,
                ) as error:
                    output.error(str(error))
                    raise typer.Exit(code=1)
                continue

            actual_task = repo_config.tasks.get("logs", "logs")
            env_runner = repo_config.env_runner or config.env_runner
            cwd = repo_config.path
            if resolved_task is not None and name in resolved_task.worktrees:
                worktree = Path(resolved_task.worktrees[name])
                if worktree.exists():
                    cwd = worktree

            from mship.util.taskfile import taskfile_has_target

            if not taskfile_has_target(cwd, actual_task):
                output.error(
                    f"'{name}' has no '{actual_task}' task in its Taskfile.\n"
                    f"  Add a `{actual_task}:` task to {cwd}/Taskfile.yml, "
                    f"or alias it via `tasks: {{logs: <real-name>}}` "
                    f"in mothership.yaml."
                )
                raise typer.Exit(code=1)
            if all_services:
                output.print(f"[bold]── {name} ──[/bold]")
            result = shell.run_task(
                task_name="logs",
                actual_task_name=actual_task,
                cwd=cwd,
                env_runner=env_runner,
            )
            output.print(result.stdout)

import os
from typing import Optional

import typer

from mship.cli.output import Output


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
    config, task_affected: list[str],
    repos_filter: str | None, tag_filter: list[str] | None,
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


def register(app: typer.Typer, get_container):
    @app.command(name="test")
    def test_cmd(
        run_all: bool = typer.Option(False, "--all", help="Run all repos even on failure"),
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names to filter"),
        tag: Optional[list[str]] = typer.Option(None, "--tag", help="Filter repos by tag"),
        no_diff: bool = typer.Option(False, "--no-diff", help="Skip cross-run diff output"),
        task: Optional[str] = typer.Option(None, "--task", help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var."),
    ):
        """Run tests across affected repos; show diff vs. previous iteration."""
        from datetime import datetime, timezone
        from mship.cli._resolve import resolve_for_command
        from mship.core.test_history import (
            write_run, read_run, latest_iteration, compute_diff, prune,
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
            if prev_iter and prev_iter > 1 else None
        )

        started_at = datetime.now(timezone.utc)

        executor = container.executor()
        result = executor.execute(
            "test", repos=target_repos, run_all=run_all,
            task_slug=t.slug,
        )

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
            per_repo[r.repo] = {
                "status": status,
                "duration_ms": r.duration_ms,
                "exit_code": r.shell_result.returncode,
                "stderr_tail": stderr_tail,
            }
            streams[r.repo] = (
                r.shell_result.stdout or "",
                r.shell_result.stderr or "",
            )

        new_iter = (prev_iter or 0) + 1
        write_run(
            state_dir, t.slug, iteration=new_iter,
            started_at=started_at, duration_ms=run_duration_ms,
            results=per_repo, streams=streams,
        )

        # Persist iteration on task (read-modify-write under the lock).
        def _record(s):
            s.tasks[t.slug].test_iteration = new_iter
        state_mgr.mutate(_record)

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

        if output.is_tty:
            output.print(f"[bold]Test run #{new_iter}[/bold]  ({run_duration_ms / 1000:.1f}s)")
            for repo_name, info in per_repo.items():
                status = info["status"]
                color = (
                    "green" if status == "pass"
                    else "yellow" if status == "skip"
                    else "red"
                )
                dur_s = info["duration_ms"] / 1000
                line = f"  {repo_name}: [{color}]{status}[/{color}]  ({dur_s:.1f}s)"
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
                    parts.append(f"{len(new_fail)} new failure(s) since iter #{prev_id}")
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

    @app.command(name="run")
    def run_cmd(
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names to filter"),
        tag: Optional[list[str]] = typer.Option(None, "--tag", help="Filter repos by tag"),
        task: Optional[str] = typer.Option(None, "--task", help="Narrow to one task's affected repos"),
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
        fallback_repos: list[str]
        try:
            t, _ = resolve_task(
                state,
                cli_task=task,
                env_task=_os.environ.get("MSHIP_TASK"),
                cwd=_P.cwd(),
            )
            fallback_repos = t.affected_repos
        except UnknownTaskError as e:
            known = ", ".join(sorted(state.tasks.keys())) or "(none)"
            output.error(f"Unknown task: {e.slug}. Known: {known}.")
            raise typer.Exit(1)
        except (NoActiveTaskError, AmbiguousTaskError):
            fallback_repos = list(config.repos.keys())

        try:
            target_repos = _resolve_repos(config, fallback_repos, repos, tag)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        executor = container.executor()
        result = executor.execute("run", repos=target_repos)

        def _kill_group(proc, sig):
            """Send sig to the whole process group. Cross-platform."""
            try:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(proc.pid, sig)
            except (ProcessLookupError, OSError):
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
        output.success(f"Started {len(result.background_processes)} background service(s):")
        for repo_result in result.results:
            if repo_result.background_pid is None and repo_result.healthcheck is None:
                continue
            pid_part = f"(pid {repo_result.background_pid})" if repo_result.background_pid else ""
            hc_part = f"  {repo_result.healthcheck.message}" if repo_result.healthcheck else ""
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
                    _kill_group(proc, signal.SIGKILL if os.name != "nt" else signal.SIGTERM)
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass

        output.print("All background services have exited")

    @app.command()
    def logs(
        service: Optional[str] = typer.Argument(None, help="Service name (omit with --all)"),
        all_services: bool = typer.Option(False, "--all", help="Tail logs for every service"),
        task: Optional[str] = typer.Option(None, "--task", help="Prefer this task's worktrees for cwd"),
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
        if not all_services and service is None:
            available = ", ".join(sorted(config.repos.keys()))
            output.error(f"Service name required, or pass --all. Available: {available}.")
            raise typer.Exit(code=1)

        targets = sorted(config.repos.keys()) if all_services else [service]
        for name in targets:
            if name not in config.repos:
                available = ", ".join(sorted(config.repos.keys()))
                output.error(f"Unknown service '{name}'. Available services: {available}.")
                raise typer.Exit(code=1)

        from pathlib import Path
        state_mgr = container.state_manager()
        state = state_mgr.load()
        shell = container.shell()

        # Try to resolve a task so we can prefer its worktree cwds. If no
        # anchor or multiple active tasks, silently fall back to repo paths;
        # an explicit --task / MSHIP_TASK pointing at an unknown slug errors.
        resolved_task = None
        try:
            resolved_task, _ = resolve_task(
                state,
                cli_task=task,
                env_task=_os.environ.get("MSHIP_TASK"),
                cwd=Path.cwd(),
            )
        except UnknownTaskError as e:
            known = ", ".join(sorted(state.tasks.keys())) or "(none)"
            output.error(f"Unknown task: {e.slug}. Known: {known}.")
            raise typer.Exit(1)
        except (NoActiveTaskError, AmbiguousTaskError):
            resolved_task = None

        for name in targets:
            repo = config.repos[name]
            actual_task = repo.tasks.get("logs", "logs")
            env_runner = repo.env_runner or config.env_runner

            cwd = repo.path
            if resolved_task is not None and name in resolved_task.worktrees:
                wt_path = Path(resolved_task.worktrees[name])
                if wt_path.exists():
                    cwd = wt_path

            if all_services:
                output.print(f"[bold]── {name} ──[/bold]")
            result = shell.run_task(
                task_name="logs",
                actual_task_name=actual_task,
                cwd=cwd,
                env_runner=env_runner,
            )
            output.print(result.stdout)

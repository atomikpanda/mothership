import os
import signal
import time as _time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from subprocess import Popen, TimeoutExpired
from threading import Event, Lock, Thread
from typing import TypeVar, cast

from mship.core.config import WorkspaceConfig, Dependency
from mship.core.graph import DependencyGraph
from mship.core.healthcheck import HealthcheckResult
from mship.core.state import StateManager, TestResult
from mship.util.shell import ShellRunner, ShellResult
from mship.util.stream_printer import StreamPrinter, drain_to_printer


@dataclass
class RepoResult:
    repo: str
    task_name: str
    shell_result: ShellResult
    skipped: bool = False
    background_pid: int | None = None
    healthcheck: HealthcheckResult | None = None
    duration_ms: int = 0
    # Other logical repos that shared this physical run via path-dedup (#127).
    shared_with: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        if self.skipped:
            return True
        if self.shell_result.returncode != 0:
            return False
        if self.healthcheck is not None and not self.healthcheck.ready:
            return False
        return True


@dataclass
class ExecutionResult:
    results: list[RepoResult] = field(default_factory=list)
    background_processes: list = field(default_factory=list)  # list[Popen]

    @property
    def success(self) -> bool:
        return all(r.success for r in self.results)


@dataclass
class LocalRun:
    """An ordinary local service participating in a mixed run launch."""

    result: RepoResult
    process: Popen | None
    drain_threads: list = field(default_factory=list)
    started_at: float = 0.0
    status: str = "active"
    cancelled: bool = False
    stop_lock: Lock = field(default_factory=Lock, repr=False)


@dataclass
class _TestGroup:
    """A group of repos that share a resolved test cwd (#127)."""

    cwd: Path
    members: list[str]  # all repos in the group
    skipped_members: list[str]  # subset with `test` in not_applicable
    runnable_members: list[str]  # subset that will share the actual run
    representative: (
        str | None
    )  # the one whose tasks.test is invoked; None if all skipped
    actual_task_name: str | None  # what to run as `task <X>`; None if all skipped


class TestTargetConflictError(Exception):
    """Raised when path-sharing repos resolve to different test task targets.

    Carries enough context for the CLI to render the actionable error message
    described in #127's acceptance criteria.
    """

    def __init__(
        self,
        *,
        cwd: Path,
        conflicting_repos: list[str],
        explicit_repos: list[str],
        implicit_repos: list[str],
    ) -> None:
        self.cwd = cwd
        self.conflicting_repos = sorted(conflicting_repos)
        self.explicit_repos = sorted(explicit_repos)
        self.implicit_repos = sorted(implicit_repos)
        super().__init__(
            f"Path-sharing repos at {cwd} resolve `task test` to different targets: "
            f"{', '.join(self.conflicting_repos)}"
        )


_LaunchResult = TypeVar("_LaunchResult")


class RepoExecutor:
    """Execute tasks across repos in dependency order, parallel within tiers."""

    def __init__(
        self,
        config: WorkspaceConfig,
        graph: DependencyGraph,
        state_manager: StateManager,
        shell: ShellRunner,
        healthcheck,  # HealthcheckRunner
    ) -> None:
        self._config = config
        self._graph = graph
        self._state_manager = state_manager
        self._shell = shell
        self._healthcheck = healthcheck
        self._printer: StreamPrinter | None = None

    def configure_run_output(self, repos: list[str]) -> None:
        """Enable the ordinary run stream printer for the specified repos."""
        self._printer = StreamPrinter(repos=sorted(set(repos)))

    def resolve_task_name(self, repo_name: str, canonical: str) -> str:
        repo = self._config.repos[repo_name]
        return repo.tasks.get(canonical, canonical)

    def resolve_env_runner(self, repo_name: str) -> str | None:
        repo = self._config.repos[repo_name]
        if repo.env_runner is not None:
            return repo.env_runner
        return self._config.env_runner

    def resolve_upstream_env(
        self, repo_name: str, task_slug: str | None
    ) -> dict[str, str]:
        """Compute UPSTREAM_* and UPSTREAM_*_TYPE env vars."""
        if task_slug is None:
            return {}
        state = self._state_manager.load()
        task = state.tasks.get(task_slug)
        if task is None or not task.worktrees:
            return {}

        env: dict[str, str] = {}
        repo_config = self._config.repos[repo_name]
        for dep in repo_config.depends_on:
            dep_name = dep.repo if isinstance(dep, Dependency) else dep
            dep_type = dep.type if isinstance(dep, Dependency) else "compile"
            if dep_name in task.worktrees:
                var_name = f"UPSTREAM_{dep_name.upper().replace('-', '_')}"
                env[var_name] = str(task.worktrees[dep_name])
                env[f"{var_name}_TYPE"] = dep_type
        return env

    def _plan_test_targets(
        self,
        repos: list[str],
        task_slug: str | None,
    ) -> list[_TestGroup]:
        """Group repos by resolved test cwd; validate; pick a representative per group.

        Within each group:
        - Members with `test` in `not_applicable` are filtered out (recorded skip).
        - Remaining members must agree on the effective `task test` target —
          either an explicit `tasks.test` value or the canonical `test` fallback.
          Disagreement raises TestTargetConflictError.
        - The representative is the member whose `tasks.test` is most-specific
          (explicit declaration preferred over canonical fallback), tie-broken
          by name. The representative is what `_execute_one` actually invokes.
        """
        by_cwd: dict[Path, list[str]] = {}
        order: list[Path] = []
        for r in repos:
            cwd = self._resolve_cwd(r, task_slug).resolve()
            if cwd not in by_cwd:
                order.append(cwd)
            by_cwd.setdefault(cwd, []).append(r)

        groups: list[_TestGroup] = []
        for cwd in order:
            members = by_cwd[cwd]
            skipped = [
                m for m in members if "test" in self._config.repos[m].not_applicable
            ]
            runnable = [m for m in members if m not in skipped]

            if not runnable:
                groups.append(
                    _TestGroup(
                        cwd=cwd,
                        members=members,
                        skipped_members=skipped,
                        runnable_members=[],
                        representative=None,
                        actual_task_name=None,
                    )
                )
                continue

            effective = {
                m: self._config.repos[m].tasks.get("test", "test") for m in runnable
            }
            distinct = set(effective.values())
            if len(distinct) > 1:
                explicit = [
                    m for m in runnable if "test" in self._config.repos[m].tasks
                ]
                implicit = [
                    m for m in runnable if "test" not in self._config.repos[m].tasks
                ]
                raise TestTargetConflictError(
                    cwd=cwd,
                    conflicting_repos=runnable,
                    explicit_repos=explicit,
                    implicit_repos=implicit,
                )

            actual = next(iter(distinct))
            with_explicit = sorted(
                m for m in runnable if "test" in self._config.repos[m].tasks
            )
            rep = with_explicit[0] if with_explicit else sorted(runnable)[0]

            groups.append(
                _TestGroup(
                    cwd=cwd,
                    members=members,
                    skipped_members=skipped,
                    runnable_members=runnable,
                    representative=rep,
                    actual_task_name=actual,
                )
            )
        return groups

    def _make_skip_result(self, repo_name: str, canonical_task: str) -> RepoResult:
        return RepoResult(
            repo=repo_name,
            task_name=f"(skipped: {canonical_task} not applicable)",
            shell_result=ShellResult(returncode=0, stdout="", stderr=""),
            skipped=True,
        )

    def _resolve_cwd(self, repo_name: str, task_slug: str | None) -> Path:
        """Get execution directory: worktree if available, otherwise resolved path.

        For repos with git_root set, path is resolved as parent_path / path.
        """
        repo_config = self._config.repos[repo_name]

        # If worktree exists in state, prefer it
        if task_slug:
            state = self._state_manager.load()
            task = state.tasks.get(task_slug)
            if task and repo_name in task.worktrees:
                wt_path = Path(task.worktrees[repo_name])
                if wt_path.exists():
                    return wt_path

        # No worktree: compute effective path
        if repo_config.git_root is not None:
            parent = self._config.repos[repo_config.git_root]
            return parent.path / repo_config.path
        return repo_config.path

    def _start_local_run(self, repo_name: str, task_slug: str | None) -> LocalRun:
        """Start one ordinary run task without waiting for a foreground process."""
        repo_config = self._config.repos[repo_name]
        if "run" in repo_config.not_applicable:
            return LocalRun(
                result=self._make_skip_result(repo_name, "run"),
                process=None,
                status="stopped",
            )

        actual_name = self.resolve_task_name(repo_name, "run")
        env_runner = self.resolve_env_runner(repo_name)
        command = self._shell.build_command(f"task {actual_name}", env_runner)
        started_at = _time.monotonic()
        process = self._shell.run_streaming(
            command,
            cwd=self._resolve_cwd(repo_name, task_slug),
            env=self.resolve_upstream_env(repo_name, task_slug) or None,
        )
        drain_threads: list = []
        if self._printer is not None:
            drain_threads = drain_to_printer(process, repo_name, self._printer)
        return LocalRun(
            result=RepoResult(
                repo=repo_name,
                task_name=actual_name,
                shell_result=ShellResult(returncode=0, stdout="", stderr=""),
                background_pid=(
                    process.pid if repo_config.start_mode == "background" else None
                ),
            ),
            process=process,
            drain_threads=drain_threads,
            started_at=started_at,
        )

    def _finish_local_run(self, local_run: LocalRun) -> LocalRun:
        """Wait for a foreground local run and record its terminal outcome."""
        if local_run.process is None:
            return local_run
        returncode = local_run.process.wait()
        for thread in local_run.drain_threads:
            thread.join(timeout=1.0)
        local_run.result.shell_result = ShellResult(
            returncode=returncode, stdout="", stderr=""
        )
        local_run.result.duration_ms = int(
            (_time.monotonic() - local_run.started_at) * 1000
        )
        with local_run.stop_lock:
            local_run.status = (
                "stopped" if local_run.cancelled or returncode == 0 else "failed"
            )
        return local_run

    @staticmethod
    def _signal_process(process: Popen, sig: int) -> None:
        """Signal an owned local process group, falling back to its leader."""
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, sig)
        except ProcessLookupError, OSError:
            try:
                process.send_signal(sig)
            except Exception:
                pass
        except Exception:
            pass

    def cancel_local_run(self, local_run: LocalRun) -> None:
        """Stop an ordinary process owned by a mixed launch without hanging."""
        with local_run.stop_lock:
            if local_run.cancelled:
                return
            local_run.cancelled = True
            process = local_run.process
            if process is not None and process.returncode is None:
                self._signal_process(process, signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except TimeoutExpired:
                    self._signal_process(
                        process, signal.SIGKILL if os.name != "nt" else signal.SIGTERM
                    )
                    process.wait(timeout=2)
            if local_run.status != "failed":
                local_run.status = "stopped"

    def launch_local(
        self,
        repo_name: str,
        task_slug: str | None,
        on_ready: Callable[[LocalRun], None],
        cancel_event: Event,
        on_started: Callable[[LocalRun], None],
    ) -> LocalRun:
        """Register ownership before waiting for ordinary task readiness."""
        local_run = self._start_local_run(repo_name, task_slug)
        on_started(local_run)
        try:
            if cancel_event.is_set():
                self.cancel_local_run(local_run)
                return local_run
            if local_run.result.skipped:
                on_ready(local_run)
                return local_run
            process = local_run.process
            assert process is not None
            repo_config = self._config.repos[repo_name]
            if repo_config.start_mode != "background":
                self._finish_local_run(local_run)
            if cancel_event.is_set():
                return local_run
            if not local_run.result.success:
                raise RuntimeError(f"{repo_name}: failed to start")
            if repo_config.healthcheck is not None:
                healthcheck = self._healthcheck.wait(
                    repo_config.healthcheck,
                    self._resolve_cwd(repo_name, task_slug),
                    self.resolve_env_runner(repo_name),
                    proc=process if repo_config.start_mode == "background" else None,
                )
                local_run.result.healthcheck = healthcheck
                if cancel_event.is_set():
                    return local_run
                if not healthcheck.ready:
                    local_run.status = "failed"
                    raise RuntimeError(f"{repo_name}: failed to start")
            returncode = process.poll()
            if returncode is not None and returncode != 0:
                local_run.status = "failed"
                raise RuntimeError(f"{repo_name}: failed to start")
            if cancel_event.is_set():
                return local_run
            on_ready(local_run)
            return local_run
        except BaseException:
            self.cancel_local_run(local_run)
            raise

    def _execute_one(
        self,
        repo_name: str,
        canonical_task: str,
        task_slug: str | None,
    ) -> tuple[RepoResult, object | None]:
        """Execute a single repo's task. Thread-safe.

        Returns (RepoResult, background_process_or_None).
        """
        repo_config = self._config.repos[repo_name]

        # Honor `not_applicable` (#76 / #109): if the canonical task is
        # declared not applicable for this repo, skip without invoking go-task.
        # The result is recorded as skipped (not pass/fail).
        if canonical_task in repo_config.not_applicable:
            return (self._make_skip_result(repo_name, canonical_task), None)

        if canonical_task == "run":
            local_run = self._start_local_run(repo_name, task_slug)
            if repo_config.start_mode == "background":
                return local_run.result, local_run.process
            return self._finish_local_run(local_run).result, None

        actual_name = self.resolve_task_name(repo_name, canonical_task)
        env_runner = self.resolve_env_runner(repo_name)
        upstream_env = self.resolve_upstream_env(repo_name, task_slug)
        cwd = self._resolve_cwd(repo_name, task_slug)

        # Non-run tasks (setup, test, ...) keep the capture-and-return path.
        _start = _time.monotonic()
        shell_result = self._shell.run_task(
            task_name=canonical_task,
            actual_task_name=actual_name,
            cwd=cwd,
            env_runner=env_runner,
            env=upstream_env or None,
        )
        _elapsed_ms = int((_time.monotonic() - _start) * 1000)

        return (
            RepoResult(
                repo=repo_name,
                task_name=actual_name,
                shell_result=shell_result,
                duration_ms=_elapsed_ms,
            ),
            None,
        )

    def execute(
        self,
        canonical_task: str,
        repos: list[str],
        run_all: bool = False,
        task_slug: str | None = None,
    ) -> ExecutionResult:
        result = ExecutionResult()

        # Test-mode dedup (#127): repos that resolve to the same cwd are
        # collapsed into a single physical run. Members with `not_applicable:
        # [test]` become pre-recorded skips; conflicting effective task names
        # raise TestTargetConflictError up to the CLI.
        rep_to_group: dict[str, _TestGroup] = {}
        if canonical_task == "test":
            groups = self._plan_test_targets(repos, task_slug)
            run_repos: list[str] = []
            pre_skips: list[RepoResult] = []
            for g in groups:
                if g.representative is None:
                    for m in g.members:
                        pre_skips.append(self._make_skip_result(m, "test"))
                else:
                    run_repos.append(g.representative)
                    rep_to_group[g.representative] = g
                    for m in g.skipped_members:
                        pre_skips.append(self._make_skip_result(m, "test"))
            if pre_skips:
                if task_slug:

                    def _record_pre_skips(task, _results=pre_skips):
                        now = datetime.now(timezone.utc)
                        for rr in _results:
                            task.test_results[rr.repo] = TestResult(
                                status="skip", at=now
                            )

                    try:
                        self._state_manager.mutate_task(
                            task_slug,
                            _record_pre_skips,
                        )
                    except KeyError:
                        pass
                result.results.extend(pre_skips)
            repos = run_repos

        tiers = self._graph.topo_tiers(repos)

        if canonical_task == "run":
            self.configure_run_output(repos)
        else:
            self._printer = None

        for tier in tiers:
            tier_results: list[RepoResult] = []
            tier_backgrounds: list = []
            # Map each background repo to its Popen so the healthcheck loop
            # (below) can poll the process and fast-fail on crash.
            repo_to_proc: dict[str, object] = {}

            if len(tier) == 1:
                # Single repo in tier — no threading overhead
                repo_result, bg = self._execute_one(tier[0], canonical_task, task_slug)
                tier_results.append(repo_result)
                if bg is not None:
                    tier_backgrounds.append(bg)
                    repo_to_proc[repo_result.repo] = bg
            else:
                # Multiple repos — run in parallel
                with ThreadPoolExecutor(max_workers=len(tier)) as pool:
                    futures = {
                        pool.submit(
                            self._execute_one, repo_name, canonical_task, task_slug
                        ): repo_name
                        for repo_name in tier
                    }
                    for future in as_completed(futures):
                        repo_result, bg = future.result()
                        tier_results.append(repo_result)
                        if bg is not None:
                            tier_backgrounds.append(bg)
                            repo_to_proc[repo_result.repo] = bg

            # Expand path-share groups (#127): each non-representative member
            # gets its own RepoResult sharing the rep's shell_result. The rep
            # and members carry `shared_with` listing the other repos in the
            # group, so renderers can collapse them into a single line.
            if rep_to_group:
                expanded: list[RepoResult] = []
                for r in tier_results:
                    expanded.append(r)
                    g = rep_to_group.get(r.repo)
                    if g is None or len(g.runnable_members) <= 1:
                        continue
                    others = [m for m in g.runnable_members if m != r.repo]
                    r.shared_with = others
                    for m in others:
                        expanded.append(
                            RepoResult(
                                repo=m,
                                task_name=r.task_name,
                                shell_result=r.shell_result,
                                duration_ms=r.duration_ms,
                                shared_with=[x for x in g.runnable_members if x != m],
                            )
                        )
                tier_results = expanded

            # Sort tier results for deterministic output order
            tier_results.sort(key=lambda r: r.repo)
            result.results.extend(tier_results)
            result.background_processes.extend(tier_backgrounds)

            # Run healthchecks for this tier (only for `run` canonical task)
            if canonical_task == "run":
                for repo_result in tier_results:
                    repo_config = self._config.repos[repo_result.repo]
                    if repo_config.healthcheck is None:
                        continue
                    if not repo_result.success:
                        # Task launch failed — skip healthcheck
                        continue
                    cwd = self._resolve_cwd(repo_result.repo, task_slug)
                    env_runner = self.resolve_env_runner(repo_result.repo)
                    hc_result = self._healthcheck.wait(
                        repo_config.healthcheck,
                        cwd,
                        env_runner,
                        proc=repo_to_proc.get(repo_result.repo),
                    )
                    repo_result.healthcheck = hc_result
                    if not hc_result.ready:
                        # Overwrite shell_result to surface failure message
                        repo_result.shell_result = ShellResult(
                            returncode=1,
                            stdout=repo_result.shell_result.stdout,
                            stderr=hc_result.message,
                        )

            # Batch-save test results for this tier
            if task_slug and canonical_task == "test":

                def _apply_test_results(task, _results=tier_results):
                    now = datetime.now(timezone.utc)
                    for repo_result in _results:
                        if repo_result.skipped:
                            status = "skip"
                        elif repo_result.success:
                            status = "pass"
                        else:
                            status = "fail"
                        task.test_results[repo_result.repo] = TestResult(
                            status=status,
                            at=now,
                        )

                try:
                    self._state_manager.mutate_task(
                        task_slug,
                        _apply_test_results,
                    )
                except KeyError:
                    pass

            # Fail-fast between tiers
            tier_success = all(r.success for r in tier_results)
            if not tier_success and not run_all:
                break

        return result

    def launch_profiled(
        self,
        launches: dict[
            str,
            Callable[[Callable[[_LaunchResult], None], Event], _LaunchResult],
        ],
        *,
        cancel: Callable[[_LaunchResult], None] | None = None,
        local_repos: tuple[str, ...] = (),
        task_slug: str | None = None,
        on_local_ready: Callable[[str, LocalRun], None] | None = None,
    ) -> dict[str, _LaunchResult]:
        """Run local and profiled owners through one dependency-ready scheduler.

        Local ownership is registered at spawn, separately from readiness:
        foreground tasks must finish and background tasks must pass healthchecks.
        Local processes live until the profiled launch streams end.
        """
        results: dict[str, _LaunchResult] = {}
        ready: dict[str, _LaunchResult] = {}
        acknowledged: set[str] = set()
        local_runs: dict[str, LocalRun] = {}
        local_names = set(local_repos)
        profile_names = set(launches)
        launch_names = profile_names | local_names
        events: Queue[tuple[str, str, object]] = Queue()
        inflight: dict[str, Event] = {}
        cancelled: set[str] = set()
        workers: list[Thread] = []
        terminal: set[str] = set()
        if local_repos:
            self.configure_run_output(list(local_repos))

        def stop_local(repo_name: str, run: LocalRun) -> None:
            cancelled.add(repo_name)
            inflight[repo_name].set()
            self.cancel_local_run(run)

        def stop_live() -> None:
            for repo_name, event in inflight.items():
                if repo_name in local_names or repo_name not in ready:
                    event.set()
            for repo_name, run in tuple(local_runs.items()):
                stop_local(repo_name, run)
            if cancel is not None:
                for repo_name, value in ready.items():
                    if repo_name not in cancelled:
                        cancelled.add(repo_name)
                        try:
                            cancel(value)
                        except Exception:
                            continue

        def stop_remaining_locals() -> None:
            if profile_names and profile_names <= terminal:
                for repo_name, run in tuple(local_runs.items()):
                    if (
                        repo_name not in terminal
                        and repo_name not in cancelled
                        and run.status == "active"
                        and run.process is not None
                        and run.process.returncode is None
                    ):
                        stop_local(repo_name, run)

        def process(
            repo_name: str,
            event: str,
            value: object,
            pending: set[str],
            reject_ended: bool = False,
        ) -> None:
            if event == "started":
                return
            if event in {"done", "failed"}:
                terminal.add(repo_name)
            if repo_name in local_names and repo_name in cancelled:
                pending.discard(repo_name)
                return
            if event == "failed":
                if isinstance(value, BaseException):
                    raise value
                raise RuntimeError(f"launch for {repo_name} failed")
            if event == "ready":
                acknowledged.add(repo_name)
                if repo_name in local_names:
                    if on_local_ready is not None:
                        on_local_ready(repo_name, cast(LocalRun, value))
                else:
                    ready[repo_name] = cast(_LaunchResult, value)
                pending.discard(repo_name)
                return
            if repo_name in pending:
                raise RuntimeError(
                    f"profile launch for {repo_name} ended before readiness"
                )
            status = getattr(value, "status", None)
            if repo_name in acknowledged and status in {"failed", "unknown"}:
                raise RuntimeError(
                    f"profile launch for {repo_name} failed after readiness"
                )
            if repo_name not in local_names:
                results[repo_name] = cast(_LaunchResult, value)
                if reject_ended and status is not None and status != "active":
                    raise RuntimeError(
                        f"profile launch for {repo_name} ended after readiness"
                    )

        def start(repo_name: str) -> None:
            cancel_event = Event()
            inflight[repo_name] = cancel_event

            def started(run: LocalRun) -> None:
                local_runs[repo_name] = run
                events.put((repo_name, "started", run))

            def worker() -> None:
                try:
                    if repo_name in local_names:
                        value = self.launch_local(
                            repo_name,
                            task_slug,
                            lambda run: events.put((repo_name, "ready", run)),
                            cancel_event,
                            started,
                        )
                    else:
                        value = launches[repo_name](
                            lambda run: events.put((repo_name, "ready", run)),
                            cancel_event,
                        )
                    events.put((repo_name, "done", value))
                except BaseException as error:
                    events.put((repo_name, "failed", error))

            thread = Thread(target=worker, name=f"mship-profile-{repo_name}")
            thread.start()
            workers.append(thread)

        try:
            tiers = self._graph.topo_tiers(list(launch_names))
            for tier_index, tier in enumerate(tiers):
                pending = set(tier)
                for repo_name in tier:
                    start(repo_name)
                while pending:
                    stop_remaining_locals()
                    process(*events.get(), pending=pending)
                if tier_index == len(tiers) - 1:
                    continue
                while True:
                    try:
                        process(
                            *events.get_nowait(),
                            pending=pending,
                            reject_ended=True,
                        )
                    except Empty:
                        break
            while terminal != launch_names:
                stop_remaining_locals()
                process(*events.get(), pending=set())
            for worker in workers:
                worker.join()
        except BaseException:
            stop_live()
            for worker in workers:
                worker.join(timeout=5)
            raise
        finally:
            for repo_name, run in tuple(local_runs.items()):
                stop_local(repo_name, run)
        return results

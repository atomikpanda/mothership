"""Production binding for profile discovery through the supervised remote runtime."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from secrets import token_urlsafe
from threading import Event
from typing import TYPE_CHECKING, Any

from mship.core.run_host import RunHostError, RunHostResolver
from mship.core.run_host.config import HostRegistration, registration_identity
from mship.core.run_target.backend import BackendExecutor, discover_on_host
from mship.core.run_target.models import (
    AppRun,
    BackendExecution,
    BackendResult,
    DiscoveryRequest,
    SelectedTarget,
    TargetSelectionError,
    host_endpoint_fingerprint,
    profile_revision,
)
from mship.core.run_target.selection import eligible_hosts, rank_targets

if TYPE_CHECKING:
    from mship.cli.output import Output
    from mship.core.config import WorkspaceConfig
    from mship.core.persistence.workspace_store import WorkspaceStore
    from mship.core.run_host.store import RunHostStore
    from mship.core.run_target.preferences import TargetPreferenceStore
    from mship.core.state import Task
    from mship.util.shell import ShellRunner


@dataclass(frozen=True)
class _PreparedHost:
    """One host's exact transfer result, scoped to its current endpoint."""

    run_ref_repos: tuple[str, ...]
    source_revision: str
    failure: str | None = None


class RemoteBackendExecutor:
    """Execute configured backend tasks on already selected remote hosts.

    Source certification is frozen once per selected repository and then delivered
    independently to every eligible host.  Failed host deliveries are retained as
    structured host-local failures so selection sees an incomplete inventory;
    they never cause a local fallback or a guessed source revision.
    """

    def __init__(
        self,
        *,
        task_obj: Task,
        config: WorkspaceConfig,
        shell: ShellRunner,
        output: Output,
        store: WorkspaceStore,
        event_sink: Callable[[Any], None] | None = None,
        transport: Any | None = None,
    ) -> None:
        self.task_obj = task_obj
        self.config = config
        self.shell = shell
        self.output = output
        self.store = store
        self.event_sink = event_sink
        self.transport = transport
        self._snapshots: dict[str, Any] = {}
        self._resolver = RunHostResolver(transport=transport)
        self._prepared: dict[tuple[str, str, str], _PreparedHost] = {}

    @staticmethod
    def _host_key(repo_name: str, host: HostRegistration) -> tuple[str, str, str]:
        return (repo_name, host.name, "|".join(registration_identity(host.connection)))

    @staticmethod
    def _result(
        *,
        exit_code: int | None = None,
        error_code: str | None = None,
        owner_ref: str | None = None,
        owner_generation: str | None = None,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> BackendResult:
        return BackendResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            error_code=error_code,
            owner_ref=owner_ref,
            owner_generation=owner_generation,
            artifacts=(),
        )

    def prepare(self, hosts: Sequence[HostRegistration], repo_name: str) -> str:
        """Freeze one source revision and transfer it to every eligible host.

        The returned revision is suitable for both the trusted backend fingerprint
        and the profile fingerprint.  Per-host failures are cached for ``__call__``
        to report as materialization failures rather than being silently skipped.
        """
        from mship.core.remote_dispatch import (
            RemoteDispatchError,
            prepare_remote_source,
            snapshot_remote_source,
        )
        from mship.core import run_transfer

        if (
            repo_name not in self.task_obj.worktrees
            or repo_name not in self.config.repos
        ):
            raise TargetSelectionError(
                "owner_unavailable", "remote task repository is unavailable"
            )
        if not hosts:
            raise TargetSelectionError("constraint_conflict", "no eligible remote host")

        snapshot = self._snapshots.get(repo_name)
        if snapshot is None:
            try:
                snapshot = snapshot_remote_source(
                    task_obj=self.task_obj,
                    target_repos=[repo_name],
                    config=self.config,
                    shell=self.shell,
                )
            except RemoteDispatchError as error:
                raise TargetSelectionError(
                    "owner_unavailable",
                    "could not certify source for remote target discovery",
                ) from error
            self._snapshots[repo_name] = snapshot
        source_revision = snapshot.source_revisions.get(repo_name)
        if not isinstance(source_revision, str):
            raise TargetSelectionError(
                "owner_unavailable",
                "could not certify source for remote target discovery",
            )

        for host in hosts:
            key = self._host_key(repo_name, host)
            cached = self._prepared.get(key)
            if cached is not None and cached.source_revision == source_revision:
                continue

            def record_transfer(git_repo: str, ref: str, sha: str) -> None:
                run_transfer.record_run_ref_receipt(
                    self.store.state_dir,
                    task=self.task_obj,
                    host=host,
                    repo=git_repo,
                    ref=ref,
                    sha=sha,
                )

            try:
                prepared = prepare_remote_source(
                    task_obj=self.task_obj,
                    target_repos=[repo_name],
                    config=self.config,
                    shell=self.shell,
                    host=host,
                    resolver=self._resolver,
                    output=self.output,
                    snapshot=snapshot,
                    on_transfer=record_transfer,
                )
                prepared_revision = prepared.source_revisions.get(repo_name)
                if prepared_revision != source_revision:
                    raise RemoteDispatchError(
                        "could not certify source identity for remote dispatch"
                    )
                self._prepared[key] = _PreparedHost(
                    run_ref_repos=prepared.run_ref_repos,
                    source_revision=source_revision,
                )
            except RemoteDispatchError, run_transfer.RunTransferError, RunHostError:
                self._prepared[key] = _PreparedHost(
                    run_ref_repos=(),
                    source_revision=source_revision,
                    failure="materialization_error",
                )
        return source_revision

    def _prepared_host(
        self, host: HostRegistration, execution: BackendExecution
    ) -> _PreparedHost | None:
        """Return a receipt prepared by the profile-resolution boundary only."""
        return self._prepared.get(self._host_key(execution.repo, host))

    @staticmethod
    def _request_matches(
        execution: BackendExecution,
        *,
        profile: Any,
        source_revision: str,
        profile_revision_value: str,
    ) -> bool:
        """Require the private request to repeat the certified execution identity."""
        request = execution.request
        if not isinstance(request, dict):
            return False
        expected = {
            "task": execution.task,
            "repo": execution.repo,
            "profile": execution.profile,
            "backend": execution.backend,
            "operation": execution.operation,
            "options": profile.options,
            "backend_revision": source_revision,
            "profile_revision": profile_revision_value,
        }
        return all(request.get(key) == value for key, value in expected.items())

    def _observation_context(
        self, host: HostRegistration, execution: BackendExecution
    ) -> tuple[dict[str, object] | None, str | None, str | None, str | None]:
        """Load only the acknowledged run bound to this exact endpoint and owner."""
        if execution.run_id is None:
            return None, None, None, None
        with self.store.read() as transaction:
            run = transaction.app_runs.get(transaction.connection, execution.run_id)
        if (
            run is None
            or run.task_slug != self.task_obj.slug
            or run.task_slug != execution.task
            or run.repo != execution.repo
            or run.profile != execution.profile
            or run.backend != execution.backend
            or run.host_name != host.name
            or run.host_scope != host.scope
            or run.host_endpoint_fingerprint
            != host_endpoint_fingerprint(
                "|".join(registration_identity(host.connection))
            )
            or run.status != "active"
            or run.owner_ref is None
            or run.owner_generation is None
            or execution.operation not in run.capabilities
        ):
            return None, None, None, None
        from mship.core.persistence.app_run_repository import PrivateBindingError

        try:
            binding = self.store.app_runs.load_private_binding(run.private_binding_ref)
        except PrivateBindingError:
            # Private binding contents are deliberately never rendered.  A missing
            # or unsafe file is identity loss, not a reason to rediscover a target.
            return None, None, None, None
        return (
            {
                "protocol_version": run.protocol_version,
                "run_id": run.id,
                "task": run.task_slug,
                "repo": run.repo,
                "profile": run.profile,
                "profile_revision": run.profile_revision,
                "backend": run.backend,
                "backend_revision": run.backend_revision,
                "host_name": run.host_name,
                "host_scope": run.host_scope,
                "host_endpoint_fingerprint": run.host_endpoint_fingerprint,
                "operation": run.operation,
                "capabilities": list(run.capabilities),
                "private_binding": binding,
            },
            run.owner_ref,
            run.owner_generation,
            run.backend_revision,
        )

    def _launch_context(
        self, host: HostRegistration, execution: BackendExecution
    ) -> dict[str, object] | None:
        """Load the private selection just persisted for a session parent."""
        if execution.run_id is None:
            return None
        with self.store.read() as transaction:
            run = transaction.app_runs.get(transaction.connection, execution.run_id)
        if (
            run is None
            or run.task_slug != self.task_obj.slug
            or run.task_slug != execution.task
            or run.repo != execution.repo
            or run.profile != execution.profile
            or run.backend != execution.backend
            or run.status != "starting"
            or run.owner_ref is not None
            or run.backend_revision != execution.request.get("backend_revision")
            or run.profile_revision != execution.request.get("profile_revision")
            or run.host_name != host.name
            or run.host_scope != host.scope
            or run.host_endpoint_fingerprint
            != host_endpoint_fingerprint(
                "|".join(registration_identity(host.connection))
            )
        ):
            return None
        from mship.core.persistence.app_run_repository import PrivateBindingError

        try:
            binding = self.store.app_runs.load_private_binding(run.private_binding_ref)
        except PrivateBindingError:
            return None
        return {
            "protocol_version": run.protocol_version,
            "run_id": run.id,
            "task": run.task_slug,
            "repo": run.repo,
            "profile": run.profile,
            "profile_revision": run.profile_revision,
            "backend": run.backend,
            "backend_revision": run.backend_revision,
            "host_name": run.host_name,
            "host_scope": run.host_scope,
            "host_endpoint_fingerprint": run.host_endpoint_fingerprint,
            "operation": run.operation,
            "capabilities": list(run.capabilities),
            "private_binding": binding,
        }

    def __call__(
        self,
        host: HostRegistration,
        execution: BackendExecution,
        *,
        event_sink: Callable[[Any], None] | None = None,
        cancel_event: Event | None = None,
    ) -> BackendResult:
        """Map one owner-side execution policy to the typed remote tool route."""
        from mship.core import run_transfer
        from mship.core.remote_client import exec_tool
        from mship.core.remote_tool import ToolRequest

        repo_config = self.config.repos.get(execution.repo)
        if (
            execution.task != self.task_obj.slug
            or execution.repo not in self.task_obj.worktrees
            or repo_config is None
        ):
            return self._result(error_code="invalid")
        profile = repo_config.run_profiles.get(execution.profile)
        if profile is None or profile.backend != execution.backend:
            return self._result(error_code="invalid")
        backend = repo_config.run_backends.get(profile.backend)
        if backend is None:
            return self._result(error_code="invalid")
        if (
            execution.preparation == "observe"
            and execution.operation not in backend.operations
        ):
            return self._result(error_code="invalid")
        expected_task = (
            backend.discover_task
            if execution.preparation == "discover"
            else backend.operations.get(execution.operation)
        )
        if execution.logical_task != expected_task:
            return self._result(error_code="invalid")
        if (
            execution.logical_task not in repo_config.tasks
            and backend.builtin_operation(execution.logical_task) is None
        ):
            return self._result(error_code="invalid")

        input_files: dict[str, str] = {}
        owner_ref = None
        owner_generation = None
        run_ref_repos: tuple[str, ...] = ()
        source_revision: str | None = None
        stored_profile_revision: str | None = None

        if execution.preparation == "observe":
            context, owner_ref, owner_generation, source_revision = (
                self._observation_context(host, execution)
            )
            if context is None or source_revision is None:
                return self._result(error_code="identity_lost")
            profile_revision_value = context.get("profile_revision")
            if not isinstance(profile_revision_value, str):
                return self._result(error_code="identity_lost")
            stored_profile_revision = profile_revision_value
        else:
            prepared = self._prepared_host(host, execution)
            if prepared is None or prepared.failure is not None:
                return self._result(error_code="materialization_error")
            run_ref_repos = prepared.run_ref_repos
            source_revision = prepared.source_revision

        if source_revision is None:
            return self._result(error_code="identity_lost")
        profile_revision_value = (
            stored_profile_revision
            if stored_profile_revision is not None
            else profile_revision(
                profile, backend, prepared_source_revision=source_revision
            )
        )
        if not self._request_matches(
            execution,
            profile=profile,
            source_revision=source_revision,
            profile_revision_value=profile_revision_value,
        ):
            return self._result(error_code="invalid")
        if execution.preparation == "launch" and execution.run_id is not None:
            context = self._launch_context(host, execution)
            if context is None:
                return self._result(error_code="identity_lost")
            input_files["MSHIP_TARGET_CONTEXT_FILE"] = json.dumps(
                context, separators=(",", ":"), sort_keys=True
            )
        try:
            input_files["MSHIP_TARGET_REQUEST_FILE"] = json.dumps(
                execution.request, separators=(",", ":"), sort_keys=True
            )
            request = ToolRequest(
                task=execution.task,
                repo=execution.repo,
                argv=(),
                task_key=execution.logical_task,
                input_files=input_files,
                preparation=execution.preparation,
                run_ref_repos=run_ref_repos,
                source_revision=source_revision,
                owner_ref=owner_ref,
                generation=owner_generation,
                max_stdout_bytes=execution.max_stdout_bytes,
                max_stderr_bytes=execution.max_stderr_bytes,
                timeout_seconds=execution.timeout_seconds,
            )
        except TypeError, ValueError, UnicodeError:
            return self._result(error_code="invalid")
        if execution.preparation != "observe":
            try:
                run_transfer.record_remote_worktree_receipt(
                    self.store.state_dir,
                    task=self.task_obj,
                    host=host,
                    repo=repo_config.git_root or execution.repo,
                    sha=source_revision,
                )
            except run_transfer.RunTransferError:
                return self._result(error_code="cleanup_receipt_error")


        session_source_revision: Callable[[str, str], str | None] | None = None
        if execution.preparation == "launch" and execution.run_id is not None:

            def session_source_revision(owner_ref: str, generation: str) -> str | None:
                with self.store.read() as transaction:
                    current = transaction.app_runs.get(
                        transaction.connection, execution.run_id
                    )
                if (
                    current is None
                    or current.owner_ref != owner_ref
                    or current.owner_generation != generation
                ):
                    return None
                return current.backend_revision

        try:
            result = exec_tool(
                request=request,
                host=host,
                resolver=self._resolver,
                event_sink=self.event_sink if event_sink is None else event_sink,
                transport=self.transport,
                session_source_revision=session_source_revision,
                cancel_event=cancel_event,
            )
        except RunHostError:
            return self._result(error_code="auth_error")
        stdout = result.stdout if execution.preparation == "discover" else b""
        stderr = result.stderr if execution.preparation == "discover" else b""
        if result.status in {"completed", "running"}:
            return self._result(
                exit_code=result.exit_code,
                owner_ref=result.owner_ref,
                owner_generation=result.generation,
                stdout=stdout,
                stderr=stderr,
            )
        return self._result(
            error_code=result.status,
            owner_ref=result.owner_ref,
            owner_generation=result.generation,
            stdout=stdout,
            stderr=stderr,
        )

    def revalidate_selected(
        self,
        selected: SelectedTarget,
        *,
        repo_name: str,
        profile_name: str,
    ) -> None:
        """Confirm the exact discovered target still exists before launch.

        This intentionally interrogates only the selected host and compares the
        sealed candidate identity.  It never reranks candidates or substitutes
        another target after a selected target disappears.
        """
        repo = self.config.repos.get(repo_name)
        if repo is None or repo_name not in self.task_obj.worktrees:
            raise TargetSelectionError(
                "owner_unavailable", "task repository is unavailable"
            )
        profile = repo.run_profiles.get(profile_name)
        backend = (
            repo.run_backends.get(profile.backend) if profile is not None else None
        )
        if profile is None or backend is None or "run" not in backend.operations:
            raise TargetSelectionError(
                "identity_lost", "selected profile is no longer available"
            )
        expected_revision = profile_revision(
            profile, backend, prepared_source_revision=selected.backend_revision
        )
        if selected.profile_revision != expected_revision:
            raise TargetSelectionError(
                "identity_lost", "selected profile identity changed"
            )
        request = DiscoveryRequest(
            protocol_version=1,
            backend=profile.backend,
            backend_revision=selected.backend_revision,
            profile=profile_name,
            profile_revision=selected.profile_revision,
            task=self.task_obj.slug,
            repo=repo_name,
            operation="run",
            options=profile.options,
            target_alias=None,
        )
        inventory = discover_on_host(selected.host, request, backend, execute=self)
        if inventory.error is not None:
            raise TargetSelectionError(
                "identity_lost", "selected target could not be revalidated"
            )
        matching = [
            candidate
            for candidate in inventory.candidates
            if candidate.target_key == selected.candidate.target_key
            and candidate.binding == selected.candidate.binding
            and candidate.ready
            and "run" in candidate.capabilities
        ]
        if len(matching) != 1:
            raise TargetSelectionError(
                "identity_lost", "selected target is no longer available"
            )

    def launch_selected(
        self,
        selected: SelectedTarget,
        *,
        repo_name: str,
        profile_name: str,
        on_ready: Callable[[AppRun], None] | None = None,
        cancel_event: Event | None = None,
    ) -> AppRun:
        """Persist one selected session before launch, activating it only on ready."""
        repo = self.config.repos.get(repo_name)
        if repo is None or repo_name not in self.task_obj.worktrees:
            raise TargetSelectionError(
                "owner_unavailable", "task repository is unavailable"
            )
        profile = repo.run_profiles.get(profile_name)
        if profile is None:
            raise TargetSelectionError(
                "profile_missing", "requested run profile is not configured"
            )
        backend = repo.run_backends.get(profile.backend)
        if backend is None or "run" not in backend.operations:
            raise TargetSelectionError(
                "constraint_conflict", "profile backend does not support run"
            )
        if selected.profile_revision != profile_revision(
            profile, backend, prepared_source_revision=selected.backend_revision
        ):
            raise TargetSelectionError(
                "identity_lost", "selected profile identity changed"
            )
        self.revalidate_selected(
            selected,
            repo_name=repo_name,
            profile_name=profile_name,
        )

        now = datetime.now(timezone.utc)
        binding_ref = self.store.app_runs.store_private_binding(
            selected.candidate.binding
        )
        run = AppRun(
            id=token_urlsafe(24),
            task_slug=self.task_obj.slug,
            repo=repo_name,
            profile=profile_name,
            profile_revision=selected.profile_revision,
            backend=profile.backend,
            backend_revision=selected.backend_revision,
            host_name=selected.host.name,
            host_scope=selected.host.scope,
            host_endpoint_fingerprint=host_endpoint_fingerprint(
                "|".join(registration_identity(selected.host.connection))
            ),
            safe_target_label=selected.candidate.label,
            target_aliases=selected.candidate.aliases,
            private_binding_ref=binding_ref,
            operation="run",
            protocol_version=1,
            capabilities=selected.candidate.capabilities,
            owner_ref=None,
            owner_generation=None,
            status="starting",
            revision=0,
            created_at=now,
            updated_at=now,
            binary_provenance=None,
        )
        with self.store.write(immediate=True) as transaction:
            transaction.app_runs.insert(transaction.connection, run)

        request = DiscoveryRequest(
            protocol_version=1,
            backend=profile.backend,
            backend_revision=run.backend_revision,
            profile=profile_name,
            profile_revision=run.profile_revision,
            task=run.task_slug,
            repo=run.repo,
            operation="run",
            options=profile.options,
            target_alias=None,
        ).model_dump(mode="json")
        execution = BackendExecution(
            task=run.task_slug,
            repo=run.repo,
            profile=run.profile,
            backend=run.backend,
            logical_task=backend.operations["run"],
            operation="run",
            request=request,
            run_id=run.id,
            preparation="launch",
            max_stdout_bytes=None,
            max_stderr_bytes=None,
            timeout_seconds=None,
        )
        ready_seen = False
        ready_invalid = False
        acknowledged_owner: tuple[str, str] | None = None

        def current_transition(
            status: str, owner_ref: str | None, generation: str | None
        ) -> AppRun | None:
            with self.store.write(immediate=True) as transaction:
                current = transaction.app_runs.get(transaction.connection, run.id)
                if current is None:
                    return None
                if (
                    current.task_slug != run.task_slug
                    or current.repo != run.repo
                    or current.host_name != run.host_name
                    or current.host_endpoint_fingerprint
                    != run.host_endpoint_fingerprint
                ):
                    return None
                if (
                    current.status == status
                    and current.owner_ref == owner_ref
                    and current.owner_generation == generation
                ):
                    return current
                try:
                    return transaction.app_runs.transition(
                        transaction.connection,
                        run_id=current.id,
                        expected_revision=current.revision,
                        status=status,
                        owner_ref=owner_ref,
                        owner_generation=generation,
                        now=datetime.now(timezone.utc),
                    )
                except KeyError, ValueError, RuntimeError:
                    return None

        def mark_unknown() -> None:
            with self.store.read() as transaction:
                current = transaction.app_runs.get(transaction.connection, run.id)
            if current is not None:
                current_transition(
                    "unknown", current.owner_ref, current.owner_generation
                )

        def launch_event(event: Any) -> None:
            nonlocal acknowledged_owner, ready_seen, ready_invalid
            if self.event_sink is not None:
                self.event_sink(event)
            kind = getattr(event, "kind", None)
            if kind not in {"started", "ready"}:
                return
            result = getattr(event, "result", None)
            owner_ref = getattr(result, "owner_ref", None)
            generation = getattr(result, "generation", None)
            source_revision = getattr(result, "source_revision", None)
            if (
                getattr(result, "status", None) != "running"
                or not isinstance(owner_ref, str)
                or not isinstance(generation, str)
                or source_revision != run.backend_revision
            ):
                ready_invalid = True
                mark_unknown()
                return
            if kind == "started":
                acknowledged = current_transition("starting", owner_ref, generation)
                if (
                    acknowledged is None
                    or acknowledged.status != "starting"
                    or acknowledged.owner_ref != owner_ref
                    or acknowledged.owner_generation != generation
                ):
                    ready_invalid = True
                    mark_unknown()
                else:
                    acknowledged_owner = (owner_ref, generation)
                return
            with self.store.read() as transaction:
                current = transaction.app_runs.get(transaction.connection, run.id)
            if (
                acknowledged_owner is None
                or current is None
                or current.owner_ref != owner_ref
                or current.owner_generation != generation
                or acknowledged_owner != (owner_ref, generation)
            ):
                ready_invalid = True
                mark_unknown()
                return
            activated = current_transition("active", owner_ref, generation)
            if activated is None or activated.status != "active":
                ready_invalid = True
                mark_unknown()
                return
            if on_ready is not None:
                on_ready(activated)
            ready_seen = True

        result = self(
            selected.host,
            execution,
            event_sink=launch_event,
            cancel_event=cancel_event,
        )
        with self.store.read() as transaction:
            current = transaction.app_runs.get(transaction.connection, run.id)
        stopped_by_host = (
            acknowledged_owner is not None
            and result.error_code == "cancelled"
            and (result.owner_ref, result.owner_generation) == acknowledged_owner
        )
        if (
            current is not None
            and not ready_seen
            and cancel_event is not None
            and cancel_event.is_set()
            and current.owner_ref is not None
        ):
            self.cancel_run(current)
            with self.store.read() as transaction:
                current = transaction.app_runs.get(transaction.connection, run.id)
        if current is not None:
            if ready_invalid or not ready_seen:
                status = "unknown" if result.error_code is not None else "failed"
                current_transition(status, current.owner_ref, current.owner_generation)
            elif current.status == "active":
                if stopped_by_host:
                    current_transition(
                        "stopped", current.owner_ref, current.owner_generation
                    )
                elif result.error_code is not None:
                    current_transition(
                        "failed", current.owner_ref, current.owner_generation
                    )
                elif result.exit_code is not None:
                    current_transition(
                        "stopped" if result.exit_code == 0 else "failed",
                        current.owner_ref,
                        current.owner_generation,
                    )
        with self.store.read() as transaction:
            final = transaction.app_runs.get(transaction.connection, run.id)
        if final is None:
            # Close may remove the record before this stream consumes its result.
            # A caller-local cancellation has no owner receipt and is not proof.
            if (
                acknowledged_owner is not None
                and not ready_invalid
                and (
                    stopped_by_host
                    or (result.error_code is None and result.exit_code == 0)
                )
            ):
                return replace(
                    run,
                    owner_ref=acknowledged_owner[0],
                    owner_generation=acknowledged_owner[1],
                    status="stopped",
                    revision=run.revision + 1,
                    updated_at=datetime.now(timezone.utc),
                )
            raise TargetSelectionError(
                "identity_lost", "selected run is no longer available"
            )
        return final

    def cancel_run(self, run: AppRun) -> None:
        """Stop only the exact acknowledged owner for a scheduled profile run."""
        if (
            run.task_slug != self.task_obj.slug
            or run.owner_ref is None
            or run.owner_generation is None
        ):
            return
        from mship.core.remote_client import RemoteExecError, stop_session
        from mship.core.run_host import RunHostError, RunHostStore

        host = RunHostStore(self.store.state_dir).effective_hosts().get(run.host_name)
        if (
            host is None
            or host.scope != run.host_scope
            or host_endpoint_fingerprint(
                "|".join(registration_identity(host.connection))
            )
            != run.host_endpoint_fingerprint
        ):
            outcome = "unknown"
        else:
            try:
                outcome = stop_session(
                    task=run.task_slug,
                    repo=run.repo,
                    owner_ref=run.owner_ref,
                    generation=run.owner_generation,
                    source_revision=run.backend_revision,
                    host=host,
                    resolver=self._resolver,
                    transport=self.transport,
                )
            except RemoteExecError, RunHostError, ValueError:
                outcome = "unknown"
        with self.store.write(immediate=True) as transaction:
            current = transaction.app_runs.get(transaction.connection, run.id)
            if (
                current is None
                or current.owner_ref != run.owner_ref
                or current.owner_generation != run.owner_generation
                or current.status not in {"starting", "active", "updating"}
            ):
                return
            transaction.app_runs.transition(
                transaction.connection,
                run_id=current.id,
                expected_revision=current.revision,
                status="stopped" if outcome == "stopped" else "unknown",
                owner_ref=current.owner_ref,
                owner_generation=current.owner_generation,
                now=datetime.now(timezone.utc),
            )


def resolve_launch(
    *,
    config: WorkspaceConfig,
    task: Task,
    repo_name: str,
    profile_name: str,
    host_name: str | None,
    remote_role: str | None,
    target_alias: str | None,
    registry: RunHostStore,
    preferences: TargetPreferenceStore,
    execute: BackendExecutor,
    choose: Callable[[Sequence[SelectedTarget]], SelectedTarget],
) -> SelectedTarget:
    """Discover and select a profile target without launching an application."""
    repo = config.repos.get(repo_name)
    if repo is None or repo_name not in task.worktrees:
        raise TargetSelectionError(
            "owner_unavailable", "task repository is unavailable"
        )
    profile = repo.run_profiles.get(profile_name)
    if profile is None:
        raise TargetSelectionError(
            "profile_missing", "requested run profile is not configured"
        )
    backend = repo.run_backends.get(profile.backend)
    if backend is None or "run" not in backend.operations:
        raise TargetSelectionError(
            "constraint_conflict", "profile backend does not support run"
        )

    hosts = eligible_hosts(
        registry.effective_hosts(),
        allowed_roles=config.run_hosts,
        role_hosts=registry.role_hosts(),
        required=profile.hosts,
        host_name=host_name,
        remote_role=remote_role,
    )
    prepare = getattr(execute, "prepare", None)
    if not callable(prepare):
        raise TargetSelectionError(
            "owner_unavailable", "profile runtime source preparation is unavailable"
        )
    source_revision = prepare(hosts, repo_name)
    if not isinstance(source_revision, str) or not source_revision:
        raise TargetSelectionError(
            "owner_unavailable", "profile runtime did not certify a source revision"
        )
    revision = profile_revision(
        profile, backend, prepared_source_revision=source_revision
    )
    request = DiscoveryRequest(
        protocol_version=1,
        backend=profile.backend,
        backend_revision=source_revision,
        profile=profile_name,
        profile_revision=revision,
        task=task.slug,
        repo=repo_name,
        operation="run",
        options=profile.options,
        target_alias=target_alias,
    )
    inventories = tuple(
        discover_on_host(host, request, backend, execute=execute) for host in hosts
    )
    winners = rank_targets(
        inventories,
        profile_revision=revision,
        operation="run",
        preference=preferences.get(repo_name, profile_name),
        preferred_role=repo.run_host,
    )
    return winners[0] if len(winners) == 1 else choose(winners)

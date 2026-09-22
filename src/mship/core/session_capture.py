"""Selected-run capture inputs without a second session selector or transport."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mship.core.remote_tool import ToolRequest
from mship.core.run_host.config import HostRegistration, registration_identity
from mship.core.run_target.models import (
    AppRun,
    DiscoveryRequest,
    host_endpoint_fingerprint,
)

if TYPE_CHECKING:
    from mship.core.config import WorkspaceConfig
    from mship.core.persistence.workspace_store import WorkspaceStore
    from mship.core.state import Task


class SessionCaptureError(ValueError):
    """A recorded session cannot safely be observed."""


@dataclass(frozen=True)
class SessionCaptureSelection:
    run: AppRun
    host: HostRegistration
    operation: ToolRequest
    platform: str | None = None


def _host_for_run(run: AppRun, *, store: WorkspaceStore) -> HostRegistration:
    from mship.core.run_host import RunHostStore

    registry = RunHostStore(store.state_dir)
    host = registry.effective_hosts().get(run.host_name)
    if (
        host is None
        or host.scope != run.host_scope
        or host_endpoint_fingerprint("|".join(registration_identity(host.connection)))
        != run.host_endpoint_fingerprint
    ):
        raise SessionCaptureError("recorded run host is no longer available")
    return host


def select_session_capture(
    *,
    store: WorkspaceStore,
    config: WorkspaceConfig,
    task: Task,
    repo_name: str,
    run_id: str | None,
    platform: str | None,
    operation_name: str = "capture",
    choose: Callable[[Sequence[AppRun]], AppRun] | None = None,
    profile_name: str | None = None,
    host_name: str | None = None,
    target_alias: str | None = None,
) -> SessionCaptureSelection:
    """Select recorded identity, never discover a new target or prepare source."""
    from mship.core.persistence.app_run_repository import PrivateBindingError

    if not isinstance(operation_name, str) or not operation_name:
        raise SessionCaptureError("recorded run operation is invalid")
    with store.read() as transaction:
        if run_id is not None:
            run = transaction.app_runs.get(transaction.connection, run_id)
            candidates = [] if run is None else [run]
        else:
            candidates = transaction.app_runs.list_candidates(
                transaction.connection, task_slug=task.slug, repo=repo_name
            )
    candidates = [
        candidate
        for candidate in candidates
        if candidate.task_slug == task.slug
        and candidate.repo == repo_name
        and (profile_name is None or candidate.profile == profile_name)
        and (host_name is None or candidate.host_name == host_name)
        and (target_alias is None or target_alias in candidate.target_aliases)
    ]
    if not candidates:
        raise SessionCaptureError(
            "no matching run; establish one with mship run or pass a valid --run-id"
        )
    if any(
        candidate.status != "active"
        or candidate.owner_ref is None
        or candidate.owner_generation is None
        for candidate in candidates
    ):
        raise SessionCaptureError(
            "recorded run is uncertain or unavailable; pass an acknowledged --run-id"
        )
    if len(candidates) == 1:
        run = candidates[0]
    elif choose is not None:
        run = choose(tuple(candidates))
        if run not in candidates:
            raise SessionCaptureError(
                "selected run does not match the recorded candidates"
            )
    else:
        raise SessionCaptureError("multiple matching runs; pass --run-id <run-id>")
    repo = config.repos.get(repo_name)
    if repo is None or repo_name not in task.worktrees:
        raise SessionCaptureError("task repository is unavailable")
    profile = repo.run_profiles.get(run.profile)
    backend = repo.run_backends.get(run.backend)
    if (
        profile is None
        or profile.backend != run.backend
        or backend is None
        or operation_name not in backend.operations
        or operation_name not in run.capabilities
    ):
        raise SessionCaptureError(
            "recorded run does not support the requested operation"
        )
    try:
        binding = store.app_runs.load_private_binding(run.private_binding_ref)
    except PrivateBindingError as error:
        raise SessionCaptureError("recorded target identity is unavailable") from error
    bound_platform = binding.get("platform")
    if backend.session_owner == "android":
        bound_platform = "android"
    if not isinstance(bound_platform, str) or not bound_platform:
        platforms = repo.capture.platforms if repo.capture else []
        bound_platform = platforms[0] if len(platforms) == 1 else None
    if (
        platform is not None
        and bound_platform is not None
        and platform != bound_platform
    ):
        raise SessionCaptureError("specified platform contradicts the recorded run")
    resolved_platform = bound_platform or platform
    if operation_name == "capture" and resolved_platform is None:
        raise SessionCaptureError(
            "recorded platform is unavailable; specify --platform"
        )
    host = _host_for_run(run, store=store)
    try:
        request = DiscoveryRequest(
            protocol_version=1,
            backend=run.backend,
            backend_revision=run.backend_revision,
            profile=run.profile,
            profile_revision=run.profile_revision,
            task=run.task_slug,
            repo=run.repo,
            operation=operation_name,
            options=profile.options,
            target_alias=None,
        ).model_dump(mode="json")
        operation = ToolRequest(
            task=run.task_slug,
            repo=run.repo,
            argv=(),
            task_key=backend.operations[operation_name],
            input_files={
                "MSHIP_TARGET_REQUEST_FILE": json.dumps(
                    request,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            },
            preparation="observe",
            source_revision=run.backend_revision,
            owner_ref=run.owner_ref,
            generation=run.owner_generation,
            max_stdout_bytes=None,
            max_stderr_bytes=None,
            timeout_seconds=None,
        )
    except (TypeError, ValueError) as error:
        raise SessionCaptureError("recorded run operation is invalid") from error
    return SessionCaptureSelection(
        run=run,
        host=host,
        operation=operation,
        platform=resolved_platform,
    )

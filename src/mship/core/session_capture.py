"""Selected-run capture inputs without a second session selector or transport."""

from __future__ import annotations

import json
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
) -> SessionCaptureSelection:
    """Resolve exactly one healthy AppRun and build a context-free observe request."""
    if (
        not isinstance(operation_name, str)
        or not operation_name
        or (
            operation_name == "capture"
            and (not isinstance(platform, str) or not platform)
        )
    ):
        raise SessionCaptureError("recorded run operation is invalid")
    with store.read() as transaction:
        if run_id is None:
            candidates = transaction.app_runs.list_candidates(
                transaction.connection, task_slug=task.slug, repo=repo_name
            )
            active = [
                candidate for candidate in candidates if candidate.status == "active"
            ]
            if len(candidates) != 1 or len(active) != 1:
                if not candidates:
                    raise SessionCaptureError(
                        "no healthy matching run; establish one with mship run or pass a valid --run-id"
                    )
                raise SessionCaptureError(
                    "multiple or uncertain matching runs; pass --run-id <run-id>"
                )
            run = active[0]
        else:
            run = transaction.app_runs.get(transaction.connection, run_id)
    if (
        run is None
        or run.task_slug != task.slug
        or run.repo != repo_name
        or run.status != "active"
        or run.owner_ref is None
        or run.owner_generation is None
    ):
        raise SessionCaptureError(
            "recorded run is unavailable or no longer acknowledged"
        )
    repo = config.repos.get(repo_name)
    if repo is None:
        raise SessionCaptureError("task repository is unavailable")
    profile = repo.run_profiles.get(run.profile)
    backend = repo.run_backends.get(run.backend)
    if (
        profile is None
        or profile.backend != run.backend
        or backend is None
        or backend.session_owner is None
        or operation_name not in backend.operations
        or operation_name not in run.capabilities
    ):
        raise SessionCaptureError(
            "recorded run does not support the requested operation"
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
                    request, separators=(",", ":"), sort_keys=True
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
    return SessionCaptureSelection(run=run, host=host, operation=operation)

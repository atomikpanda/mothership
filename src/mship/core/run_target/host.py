"""Host-side validation and private inputs for profile backend tasks."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from mship.core.config import RepoConfig
from mship.core.run_target.backend import host_bindings_path, load_host_bindings
from mship.core.run_target.models import (
    DiscoveryRequest,
    TargetSelectionError,
    profile_revision,
)

TARGET_REQUEST_FILE = "MSHIP_TARGET_REQUEST_FILE"
TARGET_CONTEXT_FILE = "MSHIP_TARGET_CONTEXT_FILE"
TARGET_BINDINGS_FILE = "MSHIP_TARGET_BINDINGS_FILE"


def _decode_profile_json(payload: str) -> dict[str, object]:
    """Decode one profile payload without accepting ambiguous JSON members."""

    def reject_duplicate_members(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, value in pairs:
            if key in decoded:
                raise ValueError("duplicate profile JSON member")
            decoded[key] = value
        return decoded

    try:
        decoded = json.loads(payload, object_pairs_hook=reject_duplicate_members)
    except (
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as error:
        raise ValueError("invalid profile JSON") from error
    if not isinstance(decoded, dict):
        raise ValueError("profile JSON must be an object")
    return decoded


def validate_backend_request(
    request: DiscoveryRequest,
    *,
    task: str,
    repo: str,
    config: RepoConfig,
    task_key: str | None,
    preparation: str,
    source_revision: str | None,
) -> DiscoveryRequest:
    """Bind one strict backend request to this server's configured repository."""
    if request.task != task or request.repo != repo:
        raise ValueError("profile request identity does not match this operation")
    profile = config.run_profiles.get(request.profile)
    if profile is None or profile.backend != request.backend:
        raise ValueError("profile request does not match configured backend")
    backend = config.run_backends.get(request.backend)
    if backend is None or request.operation not in backend.operations:
        raise ValueError("profile request operation is not configured")
    expected_task = (
        backend.discover_task
        if preparation == "discover"
        else backend.operations[request.operation]
    )
    if task_key != expected_task:
        raise ValueError("profile request task does not match preparation")
    if source_revision is None or request.backend_revision != source_revision:
        raise ValueError("profile request source does not match preparation")
    if request.profile_revision != profile_revision(
        profile, backend, prepared_source_revision=source_revision
    ):
        raise ValueError("profile request revision does not match configuration")
    if request.options != profile.options:
        raise ValueError("profile request options do not match configuration")
    return request


def owner_profile_input_files(
    input_files: Mapping[str, str],
    *,
    task: str,
    repo: str,
    config: RepoConfig,
    task_key: str | None,
    preparation: str,
    source_revision: str | None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return normalized request/context plus this host's one-backend bindings.

    The returned values are contents only. The process owner materializes them in
    its private operation storage; no caller-selected file path crosses this
    boundary.
    """
    request_payload = input_files.get(TARGET_REQUEST_FILE)
    profile_keys = {
        TARGET_REQUEST_FILE,
        TARGET_CONTEXT_FILE,
        TARGET_BINDINGS_FILE,
    }
    if request_payload is None:
        if profile_keys & input_files.keys():
            raise ValueError("profile inputs require a discovery request")
        return dict(input_files)
    try:
        decoded = _decode_profile_json(request_payload)
        request = DiscoveryRequest.model_validate(decoded)
    except (RecursionError, ValidationError, ValueError, TypeError) as error:
        raise ValueError("invalid profile request") from error
    validate_backend_request(
        request,
        task=task,
        repo=repo,
        config=config,
        task_key=task_key,
        preparation=preparation,
        source_revision=source_revision,
    )
    normalized_context: str | None = None
    context_payload = input_files.get(TARGET_CONTEXT_FILE)
    if context_payload is not None:
        try:
            normalized_context = json.dumps(
                _decode_profile_json(context_payload),
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except (RecursionError, TypeError, ValueError) as error:
            raise ValueError("invalid profile context") from error
    if home is None:
        home = Path.home()
    if environ is None:
        environ = os.environ
    try:
        bindings = load_host_bindings(
            host_bindings_path(home, environ), request.backend
        )
    except TargetSelectionError as error:
        raise ValueError("invalid host bindings") from error
    files = dict(input_files)
    files[TARGET_REQUEST_FILE] = json.dumps(
        request.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False
    )
    if normalized_context is not None:
        files[TARGET_CONTEXT_FILE] = normalized_context
    files[TARGET_BINDINGS_FILE] = json.dumps(
        bindings, separators=(",", ":"), ensure_ascii=False
    )
    return files

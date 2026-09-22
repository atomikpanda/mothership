"""Authenticated route-facing projection for immutable task results."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mship.core.task_results import ResultExpired, TaskResult, TaskResultStore, VerifiedArtifact


class TaskResultService:
    def __init__(self, store: TaskResultStore) -> None:
        self._store = store

    def list_results(
        self,
        *,
        task_slug: str | None = None,
        work_item_id: str | None = None,
        repo: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if not any((task_slug, work_item_id, repo)):
            raise ValueError("at least one result selector is required")
        self._validate_selector(task_slug)
        self._validate_selector(work_item_id)
        self._validate_selector(repo)
        return [self._summary(result) for result in self._store.list(
            task_slug=task_slug, work_item_id=work_item_id, repo=repo,
            now=now or datetime.now(timezone.utc),
        )]
    def result_metadata(self, result_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        result = self._store.get(result_id, now=current)
        if result.expires_at <= current:
            raise ResultExpired("result has expired")
        return self._full(result)

    def stream_artifact(
        self, result_id: str, artifact_id: str, *, now: datetime | None = None
    ) -> VerifiedArtifact:
        return self._store.open_verified(result_id, artifact_id, now=now or datetime.now(timezone.utc))

    def expire(self, *, now: datetime | None = None) -> int:
        return self._store.expire(now=now or datetime.now(timezone.utc))

    @staticmethod
    def _validate_selector(value: str | None) -> None:
        if value is not None and (not isinstance(value, str) or not value or len(value) > 128):
            raise ValueError("invalid result selector")

    @staticmethod
    def _artifact(artifact) -> dict[str, Any]:
        return {
            "id": artifact.id, "name": artifact.name, "media_type": artifact.media_type,
            "byte_size": artifact.byte_size, "sha256": artifact.sha256,
            "availability": artifact.availability, "reason": artifact.safe_reason,
        }

    def _summary(self, result: TaskResult) -> dict[str, Any]:
        return {
            "id": result.id, "task_slug": result.task_slug, "work_item_id": result.work_item_id,
            "repo": result.repo, "logical_task": result.logical_task,
            "outcome": {"status": result.outcome.status, "exit_code": result.outcome.exit_code,
                        "finished_at": result.outcome.finished_at.isoformat()},
            "created_at": result.created_at.isoformat(), "expires_at": result.expires_at.isoformat(),
            "artifacts": [self._artifact(item) for item in result.artifacts],
        }

    def _full(self, result: TaskResult) -> dict[str, Any]:
        payload = self._summary(result)
        payload.update({
            "task_key": result.task_key, "host_name": result.host_name,
            "host_role": result.host_role,
            "host_endpoint_fingerprint": result.host_endpoint_fingerprint,
            "worktree_identity": result.worktree_identity,
            "source_revision": result.source_revision,
            "snapshot_identity": result.snapshot_identity,
            "env_runner_identity": result.env_runner_identity,
        })
        return payload

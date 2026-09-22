"""SQLite repository for immutable task-result metadata and private locators."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Connection, func, select, update

from mship.core.persistence.schema import task_result_artifacts, task_results
from mship.core.persistence.serialization import decode_datetime, encode_datetime
from mship.core.task_results import PublishedArtifact, TaskOutcome, TaskResult

if TYPE_CHECKING:
    from mship.core.persistence.workspace_store import WorkspaceStore


class TaskResultRepository:
    """Metadata persistence only; blobs remain private to ``TaskResultStore``."""

    def __init__(self, workspace_store: "WorkspaceStore") -> None:
        self._workspace_store = workspace_store

    def insert(self, result: TaskResult, locators: dict[str, str | None]) -> None:
        if set(locators) != {artifact.id for artifact in result.artifacts}:
            raise ValueError("artifact locator set does not match immutable result")
        with self._workspace_store.write(immediate=True) as transaction:
            connection = transaction.connection
            connection.execute(task_results.insert().values(**self._result_values(result)))
            connection.execute(
                task_result_artifacts.insert(),
                [
                    {
                        "id": artifact.id,
                        "result_id": result.id,
                        "ordinal": ordinal,
                        "name": artifact.name,
                        "media_type": artifact.media_type,
                        "byte_size": artifact.byte_size,
                        "sha256": artifact.sha256,
                        "availability": artifact.availability,
                        "safe_reason": artifact.safe_reason,
                        "blob_locator": locators[artifact.id],
                    }
                    for ordinal, artifact in enumerate(result.artifacts)
                ],
            )

    def get(self, workspace_id: str, result_id: str, *, now: datetime) -> TaskResult | None:
        with self._workspace_store.read() as transaction:
            row = transaction.connection.execute(
                select(task_results).where(
                    task_results.c.workspace_id == workspace_id,
                    task_results.c.id == result_id,
                )
            ).mappings().one_or_none()
            return None if row is None else self._decode(transaction.connection, row)

    def list(
        self,
        workspace_id: str,
        *,
        task_slug: str | None,
        work_item_id: str | None,
        repo: str | None,
        now: datetime,
    ) -> tuple[TaskResult, ...]:
        statement = select(task_results).where(task_results.c.workspace_id == workspace_id)
        if task_slug is not None:
            statement = statement.where(task_results.c.task_slug == task_slug)
        if work_item_id is not None:
            statement = statement.where(task_results.c.work_item_id == work_item_id)
        if repo is not None:
            statement = statement.where(task_results.c.repo == repo)
        statement = statement.order_by(task_results.c.created_at.desc(), task_results.c.id)
        with self._workspace_store.read() as transaction:
            rows = transaction.connection.execute(statement).mappings().all()
            return tuple(self._decode(transaction.connection, row) for row in rows)

    def blob_locator(self, workspace_id: str, result_id: str, artifact_id: str) -> str | None:
        with self._workspace_store.read() as transaction:
            return transaction.connection.execute(
                select(task_result_artifacts.c.blob_locator)
                .join(task_results, task_results.c.id == task_result_artifacts.c.result_id)
                .where(
                    task_results.c.workspace_id == workspace_id,
                    task_results.c.id == result_id,
                    task_result_artifacts.c.id == artifact_id,
                )
            ).scalar_one_or_none()

    def expire(self, workspace_id: str, now: datetime) -> tuple[str, ...]:
        """Atomically make bytes unavailable while retaining immutable metadata."""
        with self._workspace_store.write(immediate=True) as transaction:
            connection = transaction.connection
            expired_results = (
                select(task_results.c.id)
                .where(
                    task_results.c.workspace_id == workspace_id,
                    task_results.c.expires_at <= encode_datetime(now),
                    task_results.c.id.in_(
                        select(task_result_artifacts.c.result_id).where(
                            task_result_artifacts.c.availability == "published"
                        )
                    ),
                )
                .order_by(task_results.c.expires_at, task_results.c.id)
                .limit(256)
            )
            rows = connection.execute(
                select(task_result_artifacts.c.blob_locator).where(
                    task_result_artifacts.c.result_id.in_(expired_results),
                    task_result_artifacts.c.availability == "published",
                )
            ).scalars().all()
            connection.execute(
                update(task_result_artifacts)
                .where(
                    task_result_artifacts.c.result_id.in_(expired_results),
                    task_result_artifacts.c.availability == "published",
                )
                .values(availability="expired", blob_locator=None)
            )
            return tuple(str(locator) for locator in rows if locator is not None)

    def blob_reference_count(self, locator: str) -> int:
        with self._workspace_store.read() as transaction:
            count = transaction.connection.execute(
                select(func.count())
                .select_from(task_result_artifacts)
                .where(
                    task_result_artifacts.c.blob_locator == locator,
                    task_result_artifacts.c.availability == "published",
                )
            ).scalar_one()
            return int(count)

    def _decode(self, connection: Connection, row) -> TaskResult:
        artifacts = tuple(
            PublishedArtifact(
                id=str(item["id"]), name=str(item["name"]), media_type=str(item["media_type"]),
                byte_size=None if item["byte_size"] is None else int(item["byte_size"]),
                sha256=None if item["sha256"] is None else str(item["sha256"]),
                availability=str(item["availability"]), safe_reason=item["safe_reason"],
            )
            for item in connection.execute(
                select(task_result_artifacts)
                .where(task_result_artifacts.c.result_id == row["id"])
                .order_by(task_result_artifacts.c.ordinal)
            ).mappings()
        )
        return TaskResult(
            id=str(row["id"]), workspace_id=str(row["workspace_id"]),
            task_slug=str(row["task_slug"]), work_item_id=row["work_item_id"],
            repo=str(row["repo"]), logical_task=str(row["logical_task"]), task_key=str(row["task_key"]),
            host_name=row["host_name"], host_role=row["host_role"],
            host_endpoint_fingerprint=row["host_endpoint_fingerprint"],
            worktree_identity=str(row["worktree_identity"]), source_revision=row["source_revision"],
            snapshot_identity=row["snapshot_identity"], env_runner_identity=row["env_runner_identity"],
            outcome=TaskOutcome(str(row["outcome_status"]), row["exit_code"], decode_datetime(row["finished_at"])),
            created_at=decode_datetime(row["created_at"]), expires_at=decode_datetime(row["expires_at"]),
            artifacts=artifacts,
        )

    @staticmethod
    def _result_values(result: TaskResult) -> dict[str, object]:
        return {
            "id": result.id, "workspace_id": result.workspace_id, "task_slug": result.task_slug,
            "work_item_id": result.work_item_id, "repo": result.repo,
            "logical_task": result.logical_task, "task_key": result.task_key,
            "host_name": result.host_name, "host_role": result.host_role,
            "host_endpoint_fingerprint": result.host_endpoint_fingerprint,
            "worktree_identity": result.worktree_identity, "source_revision": result.source_revision,
            "snapshot_identity": result.snapshot_identity, "env_runner_identity": result.env_runner_identity,
            "outcome_status": result.outcome.status, "exit_code": result.outcome.exit_code,
            "finished_at": encode_datetime(result.outcome.finished_at),
            "created_at": encode_datetime(result.created_at), "expires_at": encode_datetime(result.expires_at),
        }

"""Owner-side adapter from a reaped typed tool operation to immutable results."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_urlsafe
import shutil
from mship.core.remote_tool import ToolContext, ToolResult
from mship.core.task_results import (
    SafeHostProvenance,
    TaskOutcome,
    TaskOutputDeclaration,
    TaskResultError,
    TaskResultStore,
)


@dataclass(frozen=True)
class AuthenticatedExecutionProvenance:
    """Facts established by the serving execution owner, never a request body."""

    host_name: str | None = None
    host_role: str | None = None
    host_endpoint_fingerprint: str | None = None

    def safe(self) -> SafeHostProvenance:
        return SafeHostProvenance(
            host_name=self.host_name,
            host_role=self.host_role,
            host_endpoint_fingerprint=self.host_endpoint_fingerprint,
        )


@dataclass
class TaskResultPublisher:
    store: TaskResultStore
    declaration: TaskOutputDeclaration
    context: ToolContext
    task_key: str
    result_id: str
    output_root: Path
    declaration_path: Path
    manifest_path: Path
    provenance: AuthenticatedExecutionProvenance | None = None
    work_item_id: str | None = None

    @classmethod
    def prepare(
        cls,
        *,
        store: TaskResultStore,
        declaration: TaskOutputDeclaration,
        context: ToolContext,
        task_key: str,
        output_parent: Path,
        work_item_id: str | None = None,
        provenance: AuthenticatedExecutionProvenance | None = None,
    ) -> "TaskResultPublisher":
        result_id = token_urlsafe(24)
        output_root = store.create_output_root(output_parent)
        declaration_path, manifest_path = store.write_declaration(
            output_root, declaration, result_id
        )
        return cls(
            store=store, declaration=declaration, context=context, task_key=task_key,
            result_id=result_id, output_root=output_root,
            declaration_path=declaration_path, manifest_path=manifest_path,
            work_item_id=work_item_id, provenance=provenance,
        )

    @property
    def environment(self) -> dict[str, str]:
        return {
            "MSHIP_OUTPUT_DIR": str(self.output_root),
            "MSHIP_OUTPUT_DECLARATION_FILE": str(self.declaration_path),
            "MSHIP_OUTPUT_MANIFEST": str(self.manifest_path),
        }

    def publish(self, terminal: ToolResult) -> ToolResult:
        """Persist after reaping and before operation/context cleanup."""
        outcome = _outcome(terminal)
        try:
            result = self.store.publish(
                output_root=self.output_root,
                declaration=self.declaration,
                context=self.context,
                task_key=self.task_key,
                outcome=outcome,
                result_id=self.result_id,
                work_item_id=self.work_item_id,
                provenance=None if self.provenance is None else self.provenance.safe(),
            )
        except (TaskResultError, OSError, ValueError):
            return ToolResult(
                status="evidence_error",
                owner_ref=terminal.owner_ref,
                generation=terminal.generation,
                source_revision=terminal.source_revision,
            )
        finally:
            self.cleanup()
        if outcome.status == "completed" and any(
            declared.required and artifact.availability != "published"
            for declared, artifact in zip(self.declaration.artifacts, result.artifacts, strict=True)
        ):
            return ToolResult(
                status="evidence_error",
                owner_ref=terminal.owner_ref,
                generation=terminal.generation,
                source_revision=terminal.source_revision,
                result_id=result.id,
            )
        return replace(terminal, result_id=result.id)

    def cleanup(self) -> None:
        """Discard the transient producer root after publication or rejection."""
        shutil.rmtree(self.output_root, ignore_errors=True)


def _outcome(terminal: ToolResult) -> TaskOutcome:
    finished_at = datetime.now(timezone.utc)
    if terminal.status == "completed":
        if terminal.exit_code == 0:
            return TaskOutcome("completed", 0, finished_at)
        return TaskOutcome("failed", terminal.exit_code, finished_at)
    if terminal.status == "cancelled":
        return TaskOutcome("cancelled", None, finished_at)
    return TaskOutcome("infrastructure_error", None, finished_at)

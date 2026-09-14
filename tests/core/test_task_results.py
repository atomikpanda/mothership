from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path

import pytest

from mship.core.persistence.workspace_store import WorkspaceStore
from mship.core.remote_tool import ToolContext
from mship.core.task_results import (
    DeclaredOutput,
    ResultIntegrityError,
    TaskOutcome,
    TaskOutputDeclaration,
    TaskResultStore,
)


def _store(tmp_path: Path) -> TaskResultStore:
    workspace = WorkspaceStore(tmp_path / "state")
    return TaskResultStore(workspace.state_dir, "a" * 64, workspace.task_results)


def _declaration() -> TaskOutputDeclaration:
    return TaskOutputDeclaration(
        retention_seconds=60,
        artifacts=(DeclaredOutput(name="report", relative_path="out/report.txt", media_type="text/plain"),),
    )


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        task="task", repo="repo", worktree=tmp_path,
        source_revision="a" * 40,
    )


def _manifest() -> bytes:
    return b'{"artifacts":[{"name":"report","relative_path":"out/report.txt","media_type":"text/plain","metadata":{}}]}'


def test_zero_byte_declared_output_is_immutable_and_verified(tmp_path: Path):
    store = _store(tmp_path)
    declaration = _declaration()
    root = store.create_output_root(tmp_path)
    store.write_declaration(root, declaration, "r" * 24)
    (root / "out").mkdir()
    (root / "out" / "report.txt").write_bytes(b"")
    (root / "manifest.json").write_bytes(_manifest())

    result = store.publish(
        output_root=root, declaration=declaration, context=_context(tmp_path),
        task_key="report", result_id="r" * 24,
        outcome=TaskOutcome("completed", 0, datetime.now(timezone.utc)),
    )

    artifact = result.artifacts[0]
    assert artifact.availability == "published"
    assert artifact.byte_size == 0
    assert artifact.sha256 == hashlib.sha256(b"").hexdigest()
    with store.open_verified(result.id, artifact.id) as lease:
        assert os.read(lease.fd, 1) == b""


def test_declared_path_rejects_traversal_and_manifest_cannot_select_another_file():
    with pytest.raises(ValueError):
        DeclaredOutput(name="report", relative_path="../report.txt", media_type="text/plain")
    declaration = _declaration()
    with pytest.raises(ValueError):
        declaration.parse_manifest(
            b'{"artifacts":[{"name":"report","relative_path":"other.txt","media_type":"text/plain","metadata":{}}]}'
        )


def test_verified_stream_rejects_tampered_private_blob(tmp_path: Path):
    store = _store(tmp_path)
    declaration = _declaration()
    root = store.create_output_root(tmp_path)
    store.write_declaration(root, declaration, "s" * 24)
    (root / "out").mkdir()
    (root / "out" / "report.txt").write_bytes(b"verified")
    (root / "manifest.json").write_bytes(_manifest())
    result = store.publish(
        output_root=root, declaration=declaration, context=_context(tmp_path),
        task_key="report", result_id="s" * 24,
        outcome=TaskOutcome("completed", 0, datetime.now(timezone.utc)),
    )
    artifact = result.artifacts[0]
    locator = store._repository.blob_locator(result.workspace_id, result.id, artifact.id)
    assert locator is not None
    store._blob_path(locator).write_bytes(b"tampered")

    with pytest.raises(ResultIntegrityError):
        store.open_verified(result.id, artifact.id)


@pytest.mark.parametrize("required", [True, False])
def test_missing_output_preserves_producer_outcome_and_required_policy(tmp_path, required):
    from mship.core.remote_tool import ToolResult
    from mship.core.task_result_publication import TaskResultPublisher

    store = _store(tmp_path)
    declaration = TaskOutputDeclaration(
        retention_seconds=60,
        artifacts=(DeclaredOutput(
            name="report", relative_path="out/report.txt",
            media_type="text/plain", required=required,
        ),),
    )
    publisher = TaskResultPublisher.prepare(
        store=store, declaration=declaration, context=_context(tmp_path),
        task_key="report", output_parent=tmp_path,
    )
    publisher.manifest_path.write_bytes(_manifest())
    terminal = publisher.publish(ToolResult("completed", exit_code=0))
    assert terminal.status == ("evidence_error" if required else "completed")
    saved = store.get(terminal.result_id)
    assert saved.outcome.status == "completed" and saved.outcome.exit_code == 0
    assert saved.artifacts[0].availability != "published"


def test_failed_producer_can_publish_only_declared_diagnostics(tmp_path):
    from mship.core.remote_tool import ToolResult
    from mship.core.task_result_publication import TaskResultPublisher

    store = _store(tmp_path)
    declaration = TaskOutputDeclaration(
        retention_seconds=60,
        artifacts=(DeclaredOutput(
            name="report", relative_path="out/report.txt",
            media_type="text/plain", diagnostic_on_failure=True,
        ),),
    )
    publisher = TaskResultPublisher.prepare(
        store=store, declaration=declaration, context=_context(tmp_path),
        task_key="report", output_parent=tmp_path,
    )
    (publisher.output_root / "out").mkdir()
    (publisher.output_root / "out/report.txt").write_bytes(b"failure diagnostic")
    publisher.manifest_path.write_bytes(_manifest())
    terminal = publisher.publish(ToolResult("completed", exit_code=7))
    assert terminal.exit_code == 7
    saved = store.get(terminal.result_id)
    assert saved.outcome.status == "failed" and saved.outcome.exit_code == 7
    with store.open_verified(saved.id, saved.artifacts[0].id) as lease:
        assert os.read(lease.fd, 100) == b"failure diagnostic"


def test_retention_sweeps_progress_past_already_expired_results(tmp_path):
    from datetime import timedelta

    store = _store(tmp_path)
    declaration = _declaration()
    root = store.create_output_root(tmp_path)
    (root / "out").mkdir()
    (root / "out/report.txt").write_bytes(b"shared retained bytes")
    (root / "manifest.json").write_bytes(_manifest())
    now = datetime.now(timezone.utc)
    for index in range(257):
        result = store.publish(
            output_root=root, declaration=declaration, context=_context(tmp_path),
            task_key="report", result_id=f"result-{index:024d}",
            outcome=TaskOutcome("completed", 0, now),
        )
    expired_at = now + timedelta(hours=1)
    store.expire(now=expired_at)
    store.expire(now=expired_at)
    assert store.get(result.id).artifacts[0].availability == "expired"

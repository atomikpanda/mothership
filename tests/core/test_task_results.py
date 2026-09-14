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

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from mship.core.persistence.app_run_repository import (
    AppRunCleanupBlocked,
    AppRunConflict,
    AppRunRepository,
    AppRunTransitionError,
    PrivateBindingError,
)
from mship.core.persistence.database import WorkspaceDatabase
from mship.core.persistence.workspace_store import WorkspaceStore
from mship.core.run_target.models import AppRun, host_endpoint_fingerprint
from mship.core.state import Task

NOW = datetime(2026, 9, 12, 22, 30, tzinfo=timezone.utc)


def _task(slug: str, repo: str) -> Task:
    return Task(
        slug=slug,
        description="App-run persistence test task",
        phase="dev",
        created_at=NOW,
        affected_repos=[repo],
        branch=f"feat/{slug}",
    )


def _run(
    binding_ref: str,
    *,
    run_id: str = "run-a",
    task_slug: str = "task-a",
    repo: str = "api",
    status: str = "starting",
    revision: int = 0,
    owner_ref: str | None = None,
    owner_generation: str | None = None,
    binary_provenance: dict[str, object] | None = None,
) -> AppRun:
    return AppRun(
        id=run_id,
        task_slug=task_slug,
        repo=repo,
        profile="ios-development",
        profile_revision="a" * 64,
        backend="flutter",
        backend_revision="adapter-r2",
        host_name="studio",
        host_scope="project",
        host_endpoint_fingerprint=host_endpoint_fingerprint(
            "https://studio.example.test"
        ),
        safe_target_label="iPhone 16",
        private_binding_ref=binding_ref,
        operation="run",
        protocol_version=1,
        capabilities=("logs", "run"),
        owner_ref=owner_ref,
        owner_generation=owner_generation,
        status=status,
        revision=revision,
        created_at=NOW,
        updated_at=NOW,
        binary_provenance=binary_provenance,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("owner_ref", "owner_generation"),
    [
        ("", "generation-7"),
        ("operation-42", ""),
        ("operation-42", "generation\nprivate"),
        ("x" * 1025, "generation-7"),
    ],
    ids=(
        "empty-reference",
        "empty-generation",
        "control-character",
        "oversized-reference",
    ),
)
def test_invalid_owner_acknowledgement_leaves_committable_context_unchanged(
    tmp_path: Path, owner_ref: str, owner_generation: str
) -> None:
    state_dir = tmp_path / ".mothership"
    store = WorkspaceStore(state_dir)
    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, _task("task-a", "api"))
        binding_ref = transaction.app_runs.store_private_binding({"serial": "private"})
        original = _run(binding_ref)
        transaction.app_runs.insert(transaction.connection, original)

    with store.write(immediate=True) as transaction:
        with pytest.raises(AppRunTransitionError):
            transaction.app_runs.transition(
                transaction.connection,
                run_id=original.id,
                expected_revision=0,
                status="active",
                owner_ref=owner_ref,
                owner_generation=owner_generation,
                now=NOW,
            )

    with WorkspaceStore(state_dir).read() as transaction:
        assert transaction.app_runs.get(transaction.connection, original.id) == original


def test_two_connections_cas_owner_conflict_survives_reopen(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    database = WorkspaceDatabase(state_dir)
    repository = AppRunRepository(state_dir)
    database.initialize()

    with database.write(immediate=True) as connection:
        from mship.core.persistence.task_repository import TaskRepository

        TaskRepository().insert(connection, _task("task-a", "api"))
        binding_ref = repository.store_private_binding({"serial": "private-usb-id"})
        repository.insert(connection, _run(binding_ref))

    with database.write(immediate=True) as connection:
        winner = repository.transition(
            connection,
            run_id="run-a",
            expected_revision=0,
            status="active",
            owner_ref="operation-42",
            owner_generation="generation-7",
            now=NOW,
        )

    with database.write(immediate=True) as connection:
        with pytest.raises(AppRunConflict, match="run-a"):
            repository.transition(
                connection,
                run_id="run-a",
                expected_revision=0,
                status="active",
                owner_ref="operation-other",
                owner_generation="generation-other",
                now=NOW,
            )

    database.dispose()
    reopened = WorkspaceDatabase(state_dir)
    reopened.initialize()
    with reopened.read() as connection:
        persisted = repository.get(connection, "run-a")

    assert winner.status == "active"
    assert winner.revision == 1
    assert persisted is not None
    assert persisted.status == "active"
    assert persisted.owner_ref == "operation-42"
    assert persisted.owner_generation == "generation-7"
    assert persisted.revision == 1


def test_transition_distinguishes_missing_run_from_stale_revision(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / ".mothership")

    with store.write(immediate=True) as transaction:
        with pytest.raises(KeyError, match="missing-run"):
            transaction.app_runs.transition(
                transaction.connection,
                run_id="missing-run",
                expected_revision=0,
                status="unknown",
                owner_ref=None,
                owner_generation=None,
                now=NOW,
            )


def test_candidate_lookup_is_exact_task_repo_and_retains_unknown_states(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / ".mothership")

    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, _task("task-a", "api"))
        transaction.tasks.insert(transaction.connection, _task("task-b", "web"))
        api_ref = transaction.app_runs.store_private_binding({"serial": "api"})
        web_ref = transaction.app_runs.store_private_binding({"serial": "web"})
        transaction.app_runs.insert(
            transaction.connection, _run(api_ref, run_id="starting")
        )
        transaction.app_runs.insert(
            transaction.connection,
            _run(api_ref, run_id="unknown", status="unknown"),
        )
        transaction.app_runs.insert(
            transaction.connection,
            _run(
                api_ref,
                run_id="stopped",
                status="stopped",
            ),
        )
        transaction.app_runs.insert(
            transaction.connection,
            _run(web_ref, run_id="other-task", task_slug="task-b", repo="web"),
        )

    with store.read() as transaction:
        candidates = transaction.app_runs.list_candidates(
            transaction.connection,
            task_slug="task-a",
            repo="api",
        )

    assert [run.id for run in candidates] == ["starting", "unknown"]
    assert [run.status for run in candidates] == ["starting", "unknown"]


def test_acknowledged_owner_survives_unknown_and_terminal_transitions(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / ".mothership")
    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, _task("task-a", "api"))
        binding_ref = transaction.app_runs.store_private_binding({"serial": "private"})
        transaction.app_runs.insert(transaction.connection, _run(binding_ref))

    with store.write(immediate=True) as transaction:
        with pytest.raises(AppRunTransitionError, match="active"):
            transaction.app_runs.transition(
                transaction.connection,
                run_id="run-a",
                expected_revision=0,
                status="active",
                owner_ref=None,
                owner_generation=None,
                now=NOW,
            )
        active = transaction.app_runs.transition(
            transaction.connection,
            run_id="run-a",
            expected_revision=0,
            status="active",
            owner_ref="operation-42",
            owner_generation="generation-7",
            now=NOW,
        )
        unknown = transaction.app_runs.transition(
            transaction.connection,
            run_id="run-a",
            expected_revision=active.revision,
            status="unknown",
            owner_ref="operation-42",
            owner_generation="generation-7",
            now=NOW,
        )
        with pytest.raises(
            AppRunTransitionError, match="cannot be cleared or replaced"
        ):
            transaction.app_runs.transition(
                transaction.connection,
                run_id="run-a",
                expected_revision=unknown.revision,
                status="stopped",
                owner_ref=None,
                owner_generation=None,
                now=NOW,
            )
        with pytest.raises(
            AppRunTransitionError, match="cannot be cleared or replaced"
        ):
            transaction.app_runs.transition(
                transaction.connection,
                run_id="run-a",
                expected_revision=unknown.revision,
                status="stopped",
                owner_ref="operation-other",
                owner_generation="generation-other",
                now=NOW,
            )
        stopped = transaction.app_runs.transition(
            transaction.connection,
            run_id="run-a",
            expected_revision=unknown.revision,
            status="stopped",
            owner_ref="operation-42",
            owner_generation="generation-7",
            now=NOW,
        )

    assert stopped.owner_ref == "operation-42"
    assert stopped.owner_generation == "generation-7"
    assert "owner_ref" not in stopped.public_projection()
    assert "owner_generation" not in stopped.public_projection()


def test_private_binding_is_owner_only_and_public_projection_redacts_it(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    repository = AppRunRepository(state_dir)
    private_binding = {
        "connection_url": "https://private.example.test",
        "token": "secret-token",
        "adapter_options": {"serial": "USB-SECRET"},
    }

    binding_ref = repository.store_private_binding(private_binding)

    path = state_dir / "app-run-bindings" / f"{binding_ref}.json"
    run = _run(binding_ref)
    projection = run.public_projection()

    assert repository.load_private_binding(binding_ref) == private_binding
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(binding_ref) >= 32
    assert "private_binding_ref" not in projection
    assert "https://private.example.test" not in repr(projection)
    assert "secret-token" not in repr(projection)
    assert "USB-SECRET" not in repr(projection)
    with pytest.raises(PrivateBindingError):
        repository.load_private_binding("../not-a-binding")
    assert not os.path.islink(path)


def test_task_metadata_cleanup_returns_retained_binding_handoff(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    store = WorkspaceStore(state_dir)

    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, _task("task-a", "api"))
        binding_ref = transaction.app_runs.store_private_binding({"serial": "private"})
        transaction.app_runs.insert(transaction.connection, _run(binding_ref))
        with pytest.raises(AppRunCleanupBlocked, match="starting, active, or unknown"):
            transaction.app_runs.delete_for_task(transaction.connection, "task-a")
        transaction.app_runs.transition(
            transaction.connection,
            run_id="run-a",
            expected_revision=0,
            status="stopped",
            owner_ref=None,
            owner_generation=None,
            now=NOW,
        )
        cleanup_handoff = transaction.app_runs.delete_for_task(
            transaction.connection,
            "task-a",
        )
        assert transaction.tasks.delete(transaction.connection, "task-a")

    assert cleanup_handoff == (binding_ref,)
    assert store.app_runs.load_private_binding(binding_ref) == {"serial": "private"}


def test_rollback_keeps_terminal_metadata_and_private_binding(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    store = WorkspaceStore(state_dir)
    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, _task("task-a", "api"))
        binding_ref = transaction.app_runs.store_private_binding({"serial": "private"})
        transaction.app_runs.insert(
            transaction.connection,
            _run(binding_ref, status="stopped"),
        )

    with pytest.raises(RuntimeError, match="rollback"):
        with store.write(immediate=True) as transaction:
            assert transaction.app_runs.delete_for_task(
                transaction.connection,
                "task-a",
            ) == (binding_ref,)
            raise RuntimeError("rollback")

    with store.read() as transaction:
        restored = transaction.app_runs.get(transaction.connection, "run-a")
    assert restored is not None
    assert store.app_runs.load_private_binding(binding_ref) == {"serial": "private"}


def test_shared_private_binding_is_retained_after_one_task_metadata_deletion(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / ".mothership")
    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, _task("task-a", "api"))
        transaction.tasks.insert(transaction.connection, _task("task-b", "web"))
        binding_ref = transaction.app_runs.store_private_binding({"serial": "shared"})
        transaction.app_runs.insert(
            transaction.connection,
            _run(binding_ref, run_id="run-a", status="stopped"),
        )
        transaction.app_runs.insert(
            transaction.connection,
            _run(
                binding_ref,
                run_id="run-b",
                task_slug="task-b",
                repo="web",
                status="stopped",
            ),
        )
        assert transaction.app_runs.delete_for_task(
            transaction.connection,
            "task-a",
        ) == (binding_ref,)

    with store.read() as transaction:
        surviving = transaction.app_runs.get(transaction.connection, "run-b")
    assert surviving is not None
    assert store.app_runs.load_private_binding(binding_ref) == {"serial": "shared"}


def test_untrusted_binary_provenance_is_rejected(tmp_path: Path) -> None:
    binding_ref = AppRunRepository(tmp_path / ".mothership").store_private_binding({})
    assert _run(binding_ref).public_projection()["binary_provenance"] is None

    with pytest.raises(ValueError, match="trusted build identity"):
        _run(binding_ref, binary_provenance={"source_revision": "not-proof"})


def test_task_replace_preserves_referenced_memberships_and_rejects_removal(
    tmp_path: Path,
) -> None:
    store = WorkspaceStore(tmp_path / ".mothership")
    task = Task(
        slug="task-a",
        description="Task membership regression",
        phase="dev",
        created_at=NOW,
        affected_repos=["api", "web"],
        branch="feat/task-a",
    )
    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, task)
        binding_ref = transaction.app_runs.store_private_binding({"serial": "private"})
        transaction.app_runs.insert(transaction.connection, _run(binding_ref))

    reordered = task.model_copy(
        update={
            "description": "Task metadata changed",
            "phase": "review",
            "affected_repos": ["web", "api"],
        }
    )
    with store.write(immediate=True) as transaction:
        transaction.tasks.replace(transaction.connection, reordered)
        preserved = transaction.app_runs.get(transaction.connection, "run-a")

    assert preserved is not None
    assert preserved.repo == "api"

    removed = reordered.model_copy(update={"affected_repos": ["web"]})
    with store.write(immediate=True) as transaction:
        with pytest.raises(IntegrityError):
            transaction.tasks.replace(transaction.connection, removed)
        retained_task = transaction.tasks.get(transaction.connection, "task-a")
        retained_run = transaction.app_runs.get(transaction.connection, "run-a")

    assert retained_task is not None
    assert retained_task.model_dump(mode="json") == reordered.model_dump(mode="json")
    assert retained_run is not None

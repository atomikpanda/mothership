from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

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
        host_endpoint_fingerprint=host_endpoint_fingerprint("https://studio.example.test"),
        safe_target_label="iPhone 16",
        private_binding_ref=binding_ref,
        operation="run",
        protocol_version=1,
        capabilities=("logs", "run"),
        owner_ref=None,
        owner_generation=None,
        status=status,
        revision=revision,
        created_at=NOW,
        updated_at=NOW,
        binary_provenance=None,
    )


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



def test_transition_distinguishes_missing_run_from_stale_revision(tmp_path: Path) -> None:
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


def test_candidate_lookup_is_exact_task_repo_and_retains_unknown_states(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / ".mothership")

    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, _task("task-a", "api"))
        transaction.tasks.insert(transaction.connection, _task("task-b", "web"))
        api_ref = transaction.app_runs.store_private_binding({"serial": "api"})
        web_ref = transaction.app_runs.store_private_binding({"serial": "web"})
        transaction.app_runs.insert(transaction.connection, _run(api_ref, run_id="starting"))
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
        candidates = transaction.app_runs.list_active(
            transaction.connection,
            task_slug="task-a",
            repo="api",
        )

    assert [run.id for run in candidates] == ["starting", "unknown"]
    assert [run.status for run in candidates] == ["starting", "unknown"]


def test_invalid_owner_acknowledgements_do_not_turn_unknown_into_active(tmp_path: Path) -> None:
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
        unknown = transaction.app_runs.transition(
            transaction.connection,
            run_id="run-a",
            expected_revision=0,
            status="unknown",
            owner_ref=None,
            owner_generation=None,
            now=NOW,
        )
        with pytest.raises(AppRunTransitionError, match="unknown"):
            transaction.app_runs.transition(
                transaction.connection,
                run_id="run-a",
                expected_revision=unknown.revision,
                status="active",
                owner_ref="late-owner",
                owner_generation="late-generation",
                now=NOW,
            )


def test_private_binding_is_owner_only_and_public_projection_redacts_it(tmp_path: Path) -> None:
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
def test_task_metadata_cleanup_requires_terminal_status_and_explicit_delete(
    tmp_path: Path,
) -> None:
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
        transaction.app_runs.delete_for_task(transaction.connection, "task-a")
        assert transaction.tasks.delete(transaction.connection, "task-a")

    assert not (state_dir / "app-run-bindings" / f"{binding_ref}.json").exists()

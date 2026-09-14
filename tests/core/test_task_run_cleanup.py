from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.persistence.workspace_store import WorkspaceStore
from mship.core.run_host import HostRegistration, RunHostConnection, RunHostStore
from mship.core.run_host.config import registration_identity
from mship.core.run_target.models import AppRun, host_endpoint_fingerprint
from mship.core.run_transfer import TaskRunCleanupBlocked, cleanup_task_runs
from mship.core.state import Task

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)
OWNER_A = "a" * 24
OWNER_B = "b" * 24
GENERATION_A = "c" * 24
GENERATION_B = "d" * 24
REVISION = "e" * 40


def _task(slug: str, repo: str) -> Task:
    return Task(
        slug=slug,
        description="Task run cleanup regression",
        phase="dev",
        created_at=NOW,
        affected_repos=[repo],
        branch=f"feat/{slug}",
    )


def _host(url: str = "https://studio.example.test") -> HostRegistration:
    return HostRegistration(
        name="studio",
        roles=("mobile",),
        tags=(),
        preference=0,
        connection=RunHostConnection(url=url, token="private-token"),
        scope="project",
    )


def _run(
    binding_ref: str,
    *,
    run_id: str,
    task_slug: str,
    repo: str,
    owner_ref: str,
    generation: str,
    host: HostRegistration,
    status: str = "active",
) -> AppRun:
    return AppRun(
        id=run_id,
        task_slug=task_slug,
        repo=repo,
        profile="mobile",
        profile_revision="f" * 64,
        backend="backend",
        backend_revision=REVISION,
        host_name=host.name,
        host_scope=host.scope,
        host_endpoint_fingerprint=host_endpoint_fingerprint(
            "|".join(registration_identity(host.connection))
        ),
        safe_target_label="safe target",
        private_binding_ref=binding_ref,
        operation="run",
        protocol_version=1,
        capabilities=("run",),
        owner_ref=owner_ref,
        owner_generation=generation,
        status=status,
        revision=0,
        created_at=NOW,
        updated_at=NOW,
        binary_provenance=None,
    )


def _store_host(state_dir: Path, host: HostRegistration) -> RunHostStore:
    store = RunHostStore(state_dir)
    store.set_host(host, scope="project")
    return store


def test_cleanup_stops_only_the_two_recorded_live_owners(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    store = WorkspaceStore(state_dir)
    host = _host()
    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, _task("task-a", "api"))
        first = transaction.app_runs.store_private_binding({"target": "first"})
        second = transaction.app_runs.store_private_binding({"target": "second"})
        transaction.app_runs.insert(
            transaction.connection,
            _run(
                first,
                run_id="run-first",
                task_slug="task-a",
                repo="api",
                owner_ref=OWNER_A,
                generation=GENERATION_A,
                host=host,
            ),
        )
        transaction.app_runs.insert(
            transaction.connection,
            _run(
                second,
                run_id="run-second",
                task_slug="task-a",
                repo="api",
                owner_ref=OWNER_B,
                generation=GENERATION_B,
                host=host,
            ),
        )

    calls: list[tuple[str, str, str, str]] = []

    def stop_session(**request: object) -> str:
        calls.append(
            (
                str(request["task"]),
                str(request["repo"]),
                str(request["owner_ref"]),
                str(request["generation"]),
            )
        )
        return "stopped"

    assert cleanup_task_runs(
        _task("task-a", "api"),
        workspace_store=store,
        host_store=_store_host(state_dir, host),
        warn=pytest.fail,
        stop_session=stop_session,
    ) == (first, second)
    assert calls == [
        ("task-a", "api", OWNER_A, GENERATION_A),
        ("task-a", "api", OWNER_B, GENERATION_B),
    ]
    assert not (state_dir / "app-run-bindings" / f"{first}.json").exists()
    assert not (state_dir / "app-run-bindings" / f"{second}.json").exists()


def test_cleanup_stops_acknowledged_starting_owner(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    store = WorkspaceStore(state_dir)
    host = _host()
    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, _task("task-a", "api"))
        binding = transaction.app_runs.store_private_binding({"target": "starting"})
        transaction.app_runs.insert(
            transaction.connection,
            _run(
                binding,
                run_id="run-starting",
                task_slug="task-a",
                repo="api",
                owner_ref=OWNER_A,
                generation=GENERATION_A,
                host=host,
                status="starting",
            ),
        )

    calls: list[tuple[str, str]] = []
    assert cleanup_task_runs(
        _task("task-a", "api"),
        workspace_store=store,
        host_store=_store_host(state_dir, host),
        warn=pytest.fail,
        stop_session=lambda **request: (
            calls.append((str(request["owner_ref"]), str(request["generation"])))
            or "stopped"
        ),
    ) == (binding,)
    assert calls == [(OWNER_A, GENERATION_A)]


def test_changed_host_retains_unreachable_run_without_stopping_it(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    store = WorkspaceStore(state_dir)
    recorded_host = _host()
    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, _task("task-a", "api"))
        binding = transaction.app_runs.store_private_binding({"target": "recorded"})
        transaction.app_runs.insert(
            transaction.connection,
            _run(
                binding,
                run_id="run-a",
                task_slug="task-a",
                repo="api",
                owner_ref=OWNER_A,
                generation=GENERATION_A,
                host=recorded_host,
            ),
        )

    calls: list[object] = []
    warnings: list[str] = []
    with pytest.raises(TaskRunCleanupBlocked):
        cleanup_task_runs(
            _task("task-a", "api"),
            workspace_store=store,
            host_store=_store_host(
                state_dir, _host("https://replacement.example.test")
            ),
            warn=warnings.append,
            stop_session=lambda **request: calls.append(request) or "stopped",
        )

    with store.read() as transaction:
        retained = transaction.app_runs.get(transaction.connection, "run-a")
    assert retained is not None and retained.status == "active"
    assert calls == []
    assert warnings and "host identity changed" in warnings[0]
    assert store.app_runs.load_private_binding(binding) == {"target": "recorded"}


def test_unreachable_exact_host_retains_run_and_binding(tmp_path: Path) -> None:
    from mship.core.remote_client import RemoteExecError

    state_dir = tmp_path / ".mothership"
    store = WorkspaceStore(state_dir)
    host = _host()
    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, _task("task-a", "api"))
        binding = transaction.app_runs.store_private_binding({"target": "unreachable"})
        transaction.app_runs.insert(
            transaction.connection,
            _run(
                binding,
                run_id="run-a",
                task_slug="task-a",
                repo="api",
                owner_ref=OWNER_A,
                generation=GENERATION_A,
                host=host,
            ),
        )

    with pytest.raises(TaskRunCleanupBlocked):
        cleanup_task_runs(
            _task("task-a", "api"),
            workspace_store=store,
            host_store=_store_host(state_dir, host),
            warn=lambda _message: None,
            stop_session=lambda **_request: (_ for _ in ()).throw(RemoteExecError()),
        )

    with store.read() as transaction:
        retained = transaction.app_runs.get(transaction.connection, "run-a")
    assert retained is not None and retained.status == "active"
    assert store.app_runs.load_private_binding(binding) == {"target": "unreachable"}


def test_shared_binding_survives_other_task_cleanup(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    store = WorkspaceStore(state_dir)
    host = _host()
    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, _task("task-a", "api"))
        transaction.tasks.insert(transaction.connection, _task("task-b", "web"))
        binding = transaction.app_runs.store_private_binding({"target": "shared"})
        first = _run(
            binding,
            run_id="run-a",
            task_slug="task-a",
            repo="api",
            owner_ref=OWNER_A,
            generation=GENERATION_A,
            host=host,
        )
        second = _run(
            binding,
            run_id="run-b",
            task_slug="task-b",
            repo="web",
            owner_ref=OWNER_B,
            generation=GENERATION_B,
            host=host,
        )
        transaction.app_runs.insert(transaction.connection, first)
        transaction.app_runs.insert(transaction.connection, second)
        transaction.app_runs.transition(
            transaction.connection,
            run_id=first.id,
            expected_revision=0,
            status="stopped",
            owner_ref=OWNER_A,
            owner_generation=GENERATION_A,
            now=NOW,
        )
        transaction.app_runs.transition(
            transaction.connection,
            run_id=second.id,
            expected_revision=0,
            status="stopped",
            owner_ref=OWNER_B,
            owner_generation=GENERATION_B,
            now=NOW,
        )

    assert cleanup_task_runs(
        _task("task-a", "api"),
        workspace_store=store,
        host_store=_store_host(state_dir, host),
        warn=pytest.fail,
    ) == (binding,)
    with store.read() as transaction:
        retained = transaction.app_runs.get(transaction.connection, "run-b")
    assert retained is not None
    assert store.app_runs.load_private_binding(binding) == {"target": "shared"}


def test_rollback_retains_binding_before_post_commit_cleanup(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    store = WorkspaceStore(state_dir)
    host = _host()
    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, _task("task-a", "api"))
        binding = transaction.app_runs.store_private_binding({"target": "rollback"})
        run = _run(
            binding,
            run_id="run-a",
            task_slug="task-a",
            repo="api",
            owner_ref=OWNER_A,
            generation=GENERATION_A,
            host=host,
        )
        transaction.app_runs.insert(transaction.connection, run)
        transaction.app_runs.transition(
            transaction.connection,
            run_id=run.id,
            expected_revision=0,
            status="stopped",
            owner_ref=OWNER_A,
            owner_generation=GENERATION_A,
            now=NOW,
        )

    with pytest.raises(RuntimeError, match="rollback"):
        with store.write(immediate=True) as transaction:
            transaction.app_runs.delete_for_task(transaction.connection, "task-a")
            raise RuntimeError("rollback")

    with store.write(immediate=True) as transaction:
        assert (
            transaction.app_runs.remove_unreferenced_private_bindings(
                transaction.connection, (binding,)
            )
            == ()
        )
    assert store.app_runs.load_private_binding(binding) == {"target": "rollback"}

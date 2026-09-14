from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Literal

import pytest

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.persistence.workspace_store import WorkspaceStore
from mship.core.remote_tool import ToolResult
from mship.core.run_host.config import (
    HostRegistration,
    RunHostConnection,
    registration_identity,
)
from mship.core.run_target.models import (
    AppRun,
    BackendConfig,
    BackendExecution,
    BackendResult,
    HostRequirements,
    RunProfile,
    TargetSelectionError,
    host_endpoint_fingerprint,
    profile_revision,
)
from mship.core.run_target.service import RemoteBackendExecutor, resolve_launch
from mship.core.state import Task

_SHA = "a" * 40


def _host(name: str, *, roles: tuple[str, ...] = ("mobile",)) -> HostRegistration:
    return HostRegistration(
        name=name,
        roles=roles,
        tags=(),
        preference=0,
        connection=RunHostConnection(f"https://{name}.invalid", "private"),
        scope="project",
    )


def _task(tmp_path) -> Task:
    return Task(
        slug="task",
        description="task",
        phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["app"],
        worktrees={"app": tmp_path / "app"},
        branch="feature/task",
    )


def _config(tmp_path) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace="workspace",
        run_hosts=["mobile"],
        repos={
            "app": RepoConfig(
                path=tmp_path / "app",
                type="service",
                tasks={
                    "targets": "targets",
                    "launch": "launch",
                    "logs": "logs",
                    "capture": "capture",
                },
                run_profiles={
                    "phone": RunProfile(
                        backend="native",
                        hosts=HostRequirements(roles=("mobile",)),
                        options={"flavor": "debug"},
                    )
                },
                run_backends={
                    "native": BackendConfig(
                        discover_task="targets",
                        operations={
                            "run": "launch",
                            "logs": "logs",
                            "capture": "capture",
                        },
                    )
                },
            )
        },
    )


def _profile_request(tmp_path, **changes):
    config = _config(tmp_path)
    repo = config.repos["app"]
    profile = repo.run_profiles["phone"]
    backend = repo.run_backends["native"]
    request = {
        "protocol_version": 1,
        "task": "task",
        "repo": "app",
        "profile": "phone",
        "backend": "native",
        "operation": "run",
        "options": profile.options,
        "backend_revision": _SHA,
        "profile_revision": profile_revision(
            profile, backend, prepared_source_revision=_SHA
        ),
        "target_alias": None,
    }
    request.update(changes)
    return request


def _stored_observation_executor(
    tmp_path,
    *,
    capabilities: tuple[str, ...] = ("run", "logs"),
    status: Literal["starting", "active", "stopped", "failed", "unknown"] = "active",
):
    task = _task(tmp_path)
    config = _config(tmp_path)
    host = _host("mobile")
    store = WorkspaceStore(tmp_path / ".mothership")
    profile = config.repos["app"].run_profiles["phone"]
    backend = config.repos["app"].run_backends["native"]
    now = datetime.now(timezone.utc)
    with store.write(immediate=True) as transaction:
        transaction.tasks.insert(transaction.connection, task)
        binding_ref = transaction.app_runs.store_private_binding(
            {"serial": "private-target"}
        )
        run = AppRun(
            id="run",
            task_slug=task.slug,
            repo="app",
            profile="phone",
            profile_revision=profile_revision(
                profile, backend, prepared_source_revision=_SHA
            ),
            backend="native",
            backend_revision=_SHA,
            host_name=host.name,
            host_scope=host.scope,
            host_endpoint_fingerprint=host_endpoint_fingerprint(
                "|".join(registration_identity(host.connection))
            ),
            safe_target_label="Phone",
            private_binding_ref=binding_ref,
            operation="run",
            protocol_version=1,
            capabilities=capabilities,
            owner_ref="owner-ref",
            owner_generation="generation-ref",
            status=status,
            revision=0,
            created_at=now,
            updated_at=now,
            binary_provenance=None,
        )
        transaction.app_runs.insert(transaction.connection, run)
    return (
        RemoteBackendExecutor(
            task_obj=task,
            config=config,
            shell=object(),
            output=SimpleNamespace(breadcrumb=lambda message: None),
            store=store,
        ),
        host,
    )


def _discovery_execution(tmp_path, *, request=None) -> BackendExecution:
    return BackendExecution(
        task="task",
        repo="app",
        profile="phone",
        backend="native",
        logical_task="targets",
        operation="run",
        request=_profile_request(tmp_path) if request is None else request,
        run_id=None,
        preparation="discover",
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
        timeout_seconds=5,
    )


def _result(*, backend_revision: str = _SHA) -> BackendResult:
    return BackendResult(
        exit_code=0,
        stdout=json.dumps(
            {
                "protocol_version": 1,
                "backend": "native",
                "backend_revision": backend_revision,
                "rank_schema": [],
                "candidates": [
                    {
                        "target_key": "private-target",
                        "label": "Phone",
                        "tags": [],
                        "roles": ["mobile"],
                        "aliases": ["phone"],
                        "capabilities": ["run"],
                        "ready": True,
                        "reason": None,
                        "remediation": None,
                        "preparation": [],
                        "rank": [],
                        "binding": {"serial": "private-target"},
                    }
                ],
                "errors": [],
            }
        ).encode(),
        stderr=b"",
        error_code=None,
        owner_ref=None,
        owner_generation=None,
        artifacts=(),
    )


class _Registry:
    def __init__(self, hosts):
        self._hosts = {host.name: host for host in hosts}

    def effective_hosts(self):
        return self._hosts

    def role_hosts(self):
        return {"mobile": tuple(self._hosts)}


class _Preferences:
    def get(self, repo, profile):
        assert (repo, profile) == ("app", "phone")
        return None


class _PreparedExecutor:
    def __init__(self, *, failure_host: str | None = None):
        self.failure_host = failure_host

    def prepare(self, _hosts, _repo_name):
        return _SHA

    def __call__(self, host, _execution):
        if host.name == self.failure_host:
            return BackendResult(None, b"", b"", "transport", None, None, ())
        return _result()


def test_resolve_launch_filters_hosts_before_preparation_and_has_no_launch_side_effects(
    tmp_path,
):
    executor = _PreparedExecutor()
    winner = resolve_launch(
        config=_config(tmp_path),
        task=_task(tmp_path),
        repo_name="app",
        profile_name="phone",
        host_name=None,
        remote_role=None,
        target_alias="phone",
        registry=_Registry([_host("mobile"), _host("other", roles=("other",))]),
        preferences=_Preferences(),
        execute=executor,
        choose=lambda candidates: pytest.fail("unique target must not prompt"),
    )
    assert winner.host.name == "mobile"
    assert winner.candidate.label == "Phone"


def test_resolve_launch_fails_closed_when_one_eligible_host_is_incomplete(tmp_path):
    executor = _PreparedExecutor(failure_host="second")
    with pytest.raises(TargetSelectionError) as error:
        resolve_launch(
            config=_config(tmp_path),
            task=_task(tmp_path),
            repo_name="app",
            profile_name="phone",
            host_name=None,
            remote_role=None,
            target_alias="phone",
            registry=_Registry([_host("first"), _host("second")]),
            preferences=_Preferences(),
            execute=executor,
            choose=lambda candidates: candidates[0],
        )
    assert error.value.code == "discovery_incomplete"


def test_resolve_launch_requires_a_production_source_preparer(tmp_path):
    with pytest.raises(TargetSelectionError) as error:
        resolve_launch(
            config=_config(tmp_path),
            task=_task(tmp_path),
            repo_name="app",
            profile_name="phone",
            host_name=None,
            remote_role=None,
            target_alias=None,
            registry=_Registry([_host("mobile")]),
            preferences=_Preferences(),
            execute=lambda host, execution: _result(),
            choose=lambda candidates: candidates[0],
        )
    assert error.value.code == "owner_unavailable"


def test_remote_executor_preserves_bounded_discovery_output(tmp_path, monkeypatch):
    calls = 0

    def exec_tool(**kwargs):
        nonlocal calls
        calls += 1
        return ToolResult(status="completed", exit_code=0, stdout=b"{}", stderr=b"safe")

    monkeypatch.setattr("mship.core.remote_client.exec_tool", exec_tool)
    executor = RemoteBackendExecutor(
        task_obj=_task(tmp_path),
        config=_config(tmp_path),
        shell=object(),
        output=SimpleNamespace(breadcrumb=lambda message: None),
        store=SimpleNamespace(state_dir=tmp_path / ".mothership"),
    )
    host = _host("mobile")
    executor._prepared[executor._host_key("app", host)] = SimpleNamespace(
        run_ref_repos=("app",), source_revision=_SHA, failure=None
    )

    result = executor(host, _discovery_execution(tmp_path))

    assert calls == 1
    assert result.stdout == b"{}"
    assert result.stderr == b"safe"


def test_remote_executor_rejects_forged_certified_request_before_remote_call(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "mship.core.remote_client.exec_tool",
        lambda **kwargs: pytest.fail("forged request must not reach the remote tool"),
    )
    executor = RemoteBackendExecutor(
        task_obj=_task(tmp_path),
        config=_config(tmp_path),
        shell=object(),
        output=SimpleNamespace(breadcrumb=lambda message: None),
        store=SimpleNamespace(state_dir=tmp_path / ".mothership"),
    )
    host = _host("mobile")
    executor._prepared[executor._host_key("app", host)] = SimpleNamespace(
        run_ref_repos=("app",), source_revision=_SHA, failure=None
    )

    result = executor(
        host,
        _discovery_execution(
            tmp_path, request=_profile_request(tmp_path, backend_revision="forged")
        ),
    )

    assert result.error_code == "invalid"


def test_remote_executor_requires_prepared_source_before_remote_call(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "mship.core.remote_client.exec_tool",
        lambda **kwargs: pytest.fail(
            "unprepared request must not reach the remote tool"
        ),
    )
    executor = RemoteBackendExecutor(
        task_obj=_task(tmp_path),
        config=_config(tmp_path),
        shell=object(),
        output=SimpleNamespace(breadcrumb=lambda message: None),
        store=SimpleNamespace(state_dir=tmp_path / ".mothership"),
    )

    result = executor(_host("mobile"), _discovery_execution(tmp_path))

    assert result.error_code == "materialization_error"


def test_remote_executor_observation_never_prepares_or_retargets(tmp_path, monkeypatch):
    calls = 0

    def exec_tool(**kwargs):
        nonlocal calls
        calls += 1
        return ToolResult(
            status="completed", exit_code=0, stdout=b"private", stderr=b"private"
        )

    monkeypatch.setattr("mship.core.remote_client.exec_tool", exec_tool)
    executor = RemoteBackendExecutor(
        task_obj=_task(tmp_path),
        config=_config(tmp_path),
        shell=object(),
        output=SimpleNamespace(breadcrumb=lambda message: None),
        store=SimpleNamespace(state_dir=tmp_path / ".mothership"),
    )
    executor.prepare = lambda hosts, repo: pytest.fail(
        "observe must not transfer source"
    )
    request = _profile_request(tmp_path)
    executor._observation_context = lambda host, execution: (
        {"profile_revision": request["profile_revision"]},
        "owner-ref",
        "generation-ref",
        _SHA,
    )

    result = executor(
        _host("mobile"),
        BackendExecution(
            task="task",
            repo="app",
            profile="phone",
            backend="native",
            logical_task="launch",
            operation="run",
            request=request,
            run_id="run",
            preparation="observe",
            max_stdout_bytes=None,
            max_stderr_bytes=None,
            timeout_seconds=5,
        ),
    )

    assert calls == 1
    assert result.error_code is None
    assert result.stdout == b""
    assert result.stderr == b""


def test_remote_executor_rejects_observation_with_replaced_host_scope(
    tmp_path, monkeypatch
):
    class _Read:
        def __enter__(self):
            return SimpleNamespace(
                connection=object(),
                app_runs=SimpleNamespace(get=lambda connection, run_id: run),
            )

        def __exit__(self, *unused):
            return False

    run = SimpleNamespace(
        task_slug="task",
        repo="app",
        profile="phone",
        backend="native",
        operation="run",
        host_name="mobile",
        host_scope="user",
    )
    monkeypatch.setattr(
        "mship.core.remote_client.exec_tool",
        lambda **kwargs: pytest.fail(
            "replaced host scope must not reach the remote tool"
        ),
    )
    executor = RemoteBackendExecutor(
        task_obj=_task(tmp_path),
        config=_config(tmp_path),
        shell=object(),
        output=SimpleNamespace(breadcrumb=lambda message: None),
        store=SimpleNamespace(
            state_dir=tmp_path / ".mothership",
            read=lambda: _Read(),
        ),
    )

    result = executor(
        _host("mobile"),
        BackendExecution(
            task="task",
            repo="app",
            profile="phone",
            backend="native",
            logical_task="launch",
            operation="run",
            request=_profile_request(tmp_path),
            run_id="run",
            preparation="observe",
            max_stdout_bytes=None,
            max_stderr_bytes=None,
            timeout_seconds=5,
        ),
    )

    assert result.error_code == "identity_lost"


def test_remote_executor_rejects_ungranted_observation_before_remote_call(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "mship.core.remote_client.exec_tool",
        lambda **kwargs: pytest.fail(
            "ungranted observation must not reach the remote tool"
        ),
    )
    executor, host = _stored_observation_executor(tmp_path)

    result = executor(
        host,
        BackendExecution(
            task="task",
            repo="app",
            profile="phone",
            backend="native",
            logical_task="capture",
            operation="capture",
            request=_profile_request(tmp_path, operation="capture"),
            run_id="run",
            preparation="observe",
            max_stdout_bytes=None,
            max_stderr_bytes=None,
            timeout_seconds=5,
        ),
    )

    assert result.error_code == "identity_lost"


def test_remote_executor_rejects_unknown_persisted_observation_before_remote_call(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "mship.core.remote_client.exec_tool",
        lambda **kwargs: pytest.fail(
            "unknown observation must not reach the remote tool"
        ),
    )
    executor, host = _stored_observation_executor(tmp_path, status="unknown")

    result = executor(
        host,
        BackendExecution(
            task="task",
            repo="app",
            profile="phone",
            backend="native",
            logical_task="logs",
            operation="logs",
            request=_profile_request(tmp_path, operation="logs"),
            run_id="run",
            preparation="observe",
            max_stdout_bytes=None,
            max_stderr_bytes=None,
            timeout_seconds=5,
        ),
    )

    assert result.error_code == "identity_lost"


@pytest.mark.parametrize(
    "receipt_generation", ["new-generation", None, "foreign-generation"]
)
def test_launch_reconciles_concurrent_close_only_with_exact_host_receipt(
    tmp_path, monkeypatch, receipt_generation
):
    executor, host = _stored_observation_executor(tmp_path, status="stopped")
    selected = resolve_launch(
        config=executor.config,
        task=executor.task_obj,
        repo_name="app",
        profile_name="phone",
        host_name=None,
        remote_role=None,
        target_alias="phone",
        registry=_Registry([host]),
        preferences=_Preferences(),
        execute=_PreparedExecutor(),
        choose=lambda candidates: pytest.fail("unique target must not prompt"),
    )

    def remote_operation(self, host, execution, *, event_sink=None, cancel_event=None):
        if execution.preparation == "discover":
            return _result()
        receipt = ToolResult(
            status="running",
            owner_ref="new-owner",
            generation="new-generation",
            source_revision=_SHA,
        )
        event_sink(SimpleNamespace(kind="started", result=receipt))
        event_sink(SimpleNamespace(kind="ready", result=receipt))
        # Normal close can commit deletion before the launch stream returns.
        with self.store.write(immediate=True) as transaction:
            run = transaction.app_runs.get(transaction.connection, execution.run_id)
            transaction.app_runs.transition(
                transaction.connection,
                run_id=run.id,
                expected_revision=run.revision,
                status="stopped",
                owner_ref=run.owner_ref,
                owner_generation=run.owner_generation,
                now=datetime.now(timezone.utc),
            )
            transaction.app_runs.delete_for_task(
                transaction.connection, self.task_obj.slug
            )
        return BackendResult(
            None,
            b"",
            b"",
            "cancelled",
            "new-owner" if receipt_generation is not None else None,
            receipt_generation,
            (),
        )

    monkeypatch.setattr(RemoteBackendExecutor, "__call__", remote_operation)
    if receipt_generation == "new-generation":
        completed = executor.launch_selected(
            selected, repo_name="app", profile_name="phone"
        )
        assert completed.status == "stopped"
        with executor.store.read() as transaction:
            assert (
                transaction.app_runs.get(transaction.connection, completed.id) is None
            )
    else:
        with pytest.raises(TargetSelectionError) as error:
            executor.launch_selected(selected, repo_name="app", profile_name="phone")
        assert error.value.code == "identity_lost"

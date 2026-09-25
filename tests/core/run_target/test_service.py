from __future__ import annotations

import json
import shlex
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Literal

import httpx
import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.cli.output import Output
from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.persistence.workspace_store import WorkspaceStore
from mship.core.relay.pairing import build_pair_link
from mship.core.remote_dispatch import PreparedSource, SourceSnapshot, _RunRefSource
from mship.core.remote_tool import ToolEvent, ToolResult, encode_tool_event
from mship.core.run_host.config import (
    HostRegistration,
    RelayRunHostIdentity,
    RunHostConnection,
    registration_identity,
)
from mship.core.run_host.store import RunHostStore
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


@pytest.mark.parametrize("stage", ["preflight", "snapshot"])
def test_profile_source_failure_preserves_safe_repository_context(
    tmp_path, monkeypatch, capsys, stage
):
    from mship.core import remote_preflight

    blocked = stage == "preflight"
    state = remote_preflight.RepoState(
        repo="app",
        path=tmp_path / "app",
        branch="feature/task",
        blocked_reason=remote_preflight.ORIGIN_UNREACHABLE if blocked else None,
        detail="private-git-output private-credential",
        dirty=not blocked,
        needs_push=False,
        push_reason=None,
        head_sha=_SHA,
        git_repo="app",
    )
    preflight = remote_preflight.Preflight(
        states=[state],
        blocked=[state] if blocked else [],
        dirty=[] if blocked else [state],
        to_push=[],
    )
    monkeypatch.setattr(remote_preflight, "inspect", lambda *args, **kwargs: preflight)
    executor = RemoteBackendExecutor(
        task_obj=_task(tmp_path),
        config=_config(tmp_path),
        shell=SimpleNamespace(
            run=lambda *args, **kwargs: SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="private-git-output private-credential",
            )
        ),
        output=Output(force_json=True, force_quiet=True),
        store=SimpleNamespace(state_dir=tmp_path / ".mothership"),
    )
    with pytest.raises(TargetSelectionError) as error:
        executor.prepare([_host("mobile")], "app")
    assert error.value.code == "owner_unavailable"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "app" in captured.err
    assert "private" not in captured.err


@pytest.mark.parametrize(
    "dirty_source", [True, False], ids=["source-transfer", "discovery"]
)
def test_relay_auth_failure_reports_repair_without_launch_or_secret_output(
    tmp_path, monkeypatch, capsys, dirty_source
):
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    pairings = config_home / "mothership" / "relay-pairings.json"
    pairings.parent.mkdir(parents=True)
    credential = "private-relay-account"
    pairings.write_text(
        json.dumps({"version": 1, "pairings": {"relay.invalid": credential}})
    )
    pairings.chmod(0o600)
    host = HostRegistration(
        "mobile",
        ("mobile",),
        (),
        0,
        RelayRunHostIdentity("relay.invalid", "host-1", "workspace-1", "instance-1"),
        "project",
    )
    requests = []

    def reject_pairing(request):
        requests.append((request.method, request.url.host, request.url.path))
        return httpx.Response(401, json={"detail": "untrusted private server response"})

    snapshot = SourceSnapshot(
        {"app": _SHA},
        None,
        (_RunRefSource("app", tmp_path / "app", "feature/task", _SHA),),
    )
    monkeypatch.setattr(
        "mship.core.remote_dispatch.snapshot_remote_source", lambda **kwargs: snapshot
    )
    if not dirty_source:
        monkeypatch.setattr(
            "mship.core.remote_dispatch.prepare_remote_source",
            lambda **kwargs: PreparedSource((), {"app": _SHA}),
        )
    executor = RemoteBackendExecutor(
        task_obj=_task(tmp_path),
        config=_config(tmp_path),
        shell=SimpleNamespace(
            run=lambda *args, **kwargs: pytest.fail(
                "rejected pairing must not publish source or execute"
            )
        ),
        output=Output(force_json=True, force_quiet=True),
        store=SimpleNamespace(state_dir=tmp_path / ".mothership"),
        transport=httpx.MockTransport(reject_pairing),
    )

    with pytest.raises(TargetSelectionError) as error:
        resolve_launch(
            config=_config(tmp_path),
            task=_task(tmp_path),
            repo_name="app",
            profile_name="phone",
            host_name=None,
            remote_role=None,
            target_alias=None,
            registry=_Registry([host]),
            preferences=_Preferences(),
            execute=executor,
            choose=lambda candidates: pytest.fail(
                "rejected pairing must not select a target"
            ),
        )

    assert error.value.code == "discovery_incomplete"
    assert requests == [("GET", "enroll.relay.invalid", "/hosts")]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "mship run-host pair-relay" in captured.err
    assert credential not in captured.err
    assert "untrusted private server response" not in captured.err


@pytest.mark.parametrize(
    "mode", ["relay-rejected", "relay-refreshed", "direct-rejected"]
)
def test_execution_auth_diagnostics_follow_final_retry_outcome(
    tmp_path, monkeypatch, capsys, mode
):
    host = replace(
        _host("mobile", roles=("mobile", "--secondary")),
        tags=("lab", "--untrusted"),
        preference=7,
        name="--mobile" if mode == "direct-rejected" else "mobile",
    )
    if mode != "direct-rejected":
        host = replace(
            host,
            connection=RelayRunHostIdentity(
                "relay.invalid", "host-1", "workspace-1", "instance-1"
            ),
        )
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    pairings = config_home / "mothership" / "relay-pairings.json"
    pairings.parent.mkdir(parents=True)
    pairings.write_text(
        json.dumps({"version": 1, "pairings": {"relay.invalid": "private-fleet"}})
    )
    pairings.chmod(0o600)
    monkeypatch.setattr(
        "mship.core.remote_dispatch.snapshot_remote_source",
        lambda **kwargs: SourceSnapshot({"app": _SHA}, None, ()),
    )
    monkeypatch.setattr(
        "mship.core.remote_dispatch.prepare_remote_source",
        lambda **kwargs: PreparedSource((), {"app": _SHA}),
    )
    minted = []
    executions = []

    def transport(request):
        if request.url == httpx.URL("https://enroll.relay.invalid/hosts"):
            return httpx.Response(
                200,
                json={
                    "hosts": [
                        {
                            "host_id": "host-1",
                            "state": "online",
                            "instance_id": "instance-1",
                            "subdomain": "mobile-abcdef",
                            "public_url": "https://mobile-abcdef.relay.invalid",
                            "refresh": "private-refresh",
                        }
                    ]
                },
            )
        if request.url == httpx.URL("https://mobile-abcdef.relay.invalid/host/token"):
            token = f"private-bearer-{len(minted) + 1}"
            minted.append(token)
            return httpx.Response(200, json={"token": token, "expires_in": 60})
        expected_url = (
            "https://mobile.invalid/exec/tool"
            if mode == "direct-rejected"
            else "https://mobile-abcdef.relay.invalid/workspaces/workspace-1/exec/tool"
        )
        assert str(request.url) == expected_url
        assert request.method == "POST"
        executions.append(request.headers["Authorization"])
        if len(executions) == 1 or mode != "relay-refreshed":
            return httpx.Response(
                401 if len(executions) == 1 else 403,
                content=b"untrusted private server response",
            )
        identity = {
            "owner_ref": "owner-abcdef",
            "generation": "generation-abcdef",
            "source_revision": _SHA,
        }
        nonce = "nonceabcdef012345"
        events = (
            ToolEvent("started", result=ToolResult("running", **identity)),
            ToolEvent(
                "result",
                result=ToolResult(
                    "completed", exit_code=0, stdout=_result().stdout, **identity
                ),
            ),
        )
        return httpx.Response(
            200,
            headers={"X-Mship-Exec-Nonce": nonce},
            content=iter(encode_tool_event(event, nonce) for event in events),
        )

    executor = RemoteBackendExecutor(
        task_obj=_task(tmp_path),
        config=_config(tmp_path),
        shell=SimpleNamespace(
            run=lambda *args, **kwargs: pytest.fail(
                "discovery must not execute locally"
            )
        ),
        output=Output(force_json=True, force_quiet=True),
        store=SimpleNamespace(state_dir=tmp_path / ".mothership"),
        transport=httpx.MockTransport(transport),
    )

    def select():
        return resolve_launch(
            config=_config(tmp_path),
            task=_task(tmp_path),
            repo_name="app",
            profile_name="phone",
            host_name=None,
            remote_role=None,
            target_alias=None,
            registry=_Registry([host]),
            preferences=_Preferences(),
            execute=executor,
            choose=lambda candidates: pytest.fail("one target must not prompt"),
        )

    if mode == "relay-refreshed":
        selected = select()
        assert selected.host.name == "mobile"
        assert selected.candidate.label == "Phone"
    else:
        with pytest.raises(TargetSelectionError) as error:
            select()
        assert error.value.code == "discovery_incomplete"
    if mode == "direct-rejected":
        assert len(executions) == 1
        assert not minted
    else:
        assert len(executions) == len(minted) == 2
        assert executions[0] != executions[1]
    captured = capsys.readouterr()
    assert captured.out == ""
    if mode == "relay-refreshed":
        assert captured.err == ""
    else:
        assert "mobile" in captured.err
        recovery = (
            "mship run-host add"
            if mode == "direct-rejected"
            else "mship run-host pair-relay"
        )
        assert captured.err.count(recovery) == 1
    assert "private" not in captured.err
    if mode == "direct-rejected":
        command = next(
            shlex.split(part)
            for part in captured.err.split("`")
            if part.startswith("mship run-host add ")
        )
        fresh = RunHostConnection("https://repaired.invalid", "private-repaired")
        link = build_pair_link(url=fresh.url, token=fresh.token, workspace="workspace")
        command = [
            link if part == "<fresh-direct-pair-link>" else part for part in command
        ]
        state_dir = tmp_path / ".mothership"
        state_dir.mkdir(exist_ok=True)
        config_path = tmp_path / "mothership.yaml"
        config_path.write_text("workspace: workspace\nrepos: {}\n")
        registry = RunHostStore(state_dir)
        registry.set_host(host, scope=host.scope)
        container.config.reset()
        container.state_manager.reset()
        container.config_path.override(config_path)
        container.state_dir.override(state_dir)
        try:
            repaired = CliRunner().invoke(app, command[1:])
            assert repaired.exit_code == 0, repaired.output
            assert registry.connection_for_role("--secondary", environ={}) == fresh
            registration = registry.effective_hosts()[host.name]
            assert registration.tags == ("lab", "--untrusted")
            assert registration.preference == 7
        finally:
            container.config_path.reset_override()
            container.state_dir.reset_override()
            container.config.reset()
            container.state_manager.reset()


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


@pytest.mark.parametrize(
    ("exit_code", "error_code", "exact_owner", "expected_status"),
    [
        (None, None, False, "active"),
        (0, None, False, "stopped"),
        (23, None, False, "failed"),
        (23, "cancelled", True, "stopped"),
    ],
)
def test_launch_reconciles_ready_backend_completion_status(
    tmp_path, monkeypatch, exit_code, error_code, exact_owner, expected_status
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
            owner_ref="owner",
            generation="generation",
            source_revision=_SHA,
        )
        event_sink(SimpleNamespace(kind="started", result=receipt))
        event_sink(SimpleNamespace(kind="ready", result=receipt))
        return BackendResult(
            exit_code=exit_code,
            stdout=b"",
            stderr=b"",
            error_code=error_code,
            owner_ref=receipt.owner_ref if exact_owner else None,
            owner_generation=receipt.generation if exact_owner else None,
            artifacts=(),
        )

    monkeypatch.setattr(RemoteBackendExecutor, "__call__", remote_operation)

    completed = executor.launch_selected(
        selected, repo_name="app", profile_name="phone"
    )

    assert completed.status == expected_status
    with executor.store.read() as transaction:
        persisted = transaction.app_runs.get(transaction.connection, completed.id)
    assert persisted is not None
    assert persisted.status == expected_status

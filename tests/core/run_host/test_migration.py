"""Versioned layered run-host registry migration regressions."""
from concurrent.futures import ThreadPoolExecutor

from pathlib import Path

import pytest
import yaml

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.run_host.config import HostRegistration, RunHostConnection
from mship.core.run_host.store import RunHostError, RunHostStore, resolve_run_host


def _config(*roles: str) -> WorkspaceConfig:
    return WorkspaceConfig(workspace="test", run_hosts=list(roles), repos={})


def test_project_host_replaces_roles_and_credentials(tmp_path: Path):
    user = tmp_path / "user"
    state = tmp_path / "project"
    store = RunHostStore(state, user_config_dir=user)
    store.set_host(HostRegistration(
        name="studio", roles=("ios",), tags=(), preference=10,
        connection=RunHostConnection(url="https://global.invalid", token="global"),
        scope="user"), scope="user")
    store.set_host(HostRegistration(
        name="studio", roles=("android",), tags=(), preference=0,
        connection=RunHostConnection(url="https://project.invalid", token="private"),
        scope="project"), scope="project")

    winner = store.effective_hosts()["studio"]
    assert winner.roles == ("android",)
    assert winner.connection == RunHostConnection("https://project.invalid", "private")
    assert winner.scope == "project"


def test_project_migration_preserves_exact_role_not_global_pool(tmp_path: Path):
    user = tmp_path / "user"
    state = tmp_path / "project"
    store = RunHostStore(state, user_config_dir=user)
    store.set_host(HostRegistration(
        name="air", roles=("ios",), tags=(), preference=0,
        connection=RunHostConnection("https://air.invalid", "air-token"), scope="user"),
        scope="user")
    (state / "run-hosts.yaml").parent.mkdir(parents=True)
    (state / "run-hosts.yaml").write_text(yaml.safe_dump({"ios": {"url": "https://old.invalid", "token": "old-token"}}))

    preview = store.migrate(scope="project", allowed_roles=("ios",), apply=False)
    assert preview.changed is True
    assert not (state / "run-hosts.yaml.bak").exists()
    applied = store.migrate(scope="project", allowed_roles=("ios",), apply=True)
    assert applied.changed is True
    assert store.role_hosts() == {"ios": ("ios",)}
    assert resolve_run_host("ios", repo=None, config=_config("ios"), store=store).connection == RunHostConnection("https://old.invalid", "old-token")

    store.set_role_hosts("ios", None)
    with pytest.raises(RunHostError, match="ambiguous"):
        resolve_run_host("ios", repo=None, config=_config("ios"), store=store)


def test_invalid_project_override_never_uses_shadowed_user_connection(tmp_path: Path):
    store = RunHostStore(tmp_path / "state", user_config_dir=tmp_path / "user")
    store.set_host(HostRegistration(
        name="studio", roles=("ios",), tags=(), preference=0,
        connection=RunHostConnection("https://user.invalid", "secret"), scope="user"), scope="user")
    project_path = tmp_path / "state" / "run-hosts.yaml"
    project_path.parent.mkdir(parents=True)
    project_path.write_text("version: 2\nhosts:\n  studio:\n    roles: [ios]\n")

    with pytest.raises(RunHostError, match="invalid.*project") as error:
        resolve_run_host("ios", repo=None, config=_config("ios"), store=store)
    assert "secret" not in str(error.value)


def test_role_env_override_requires_single_allowed_host(tmp_path: Path, monkeypatch):
    store = RunHostStore(tmp_path / "state", user_config_dir=tmp_path / "user")
    for name in ("one", "two"):
        store.set_host(HostRegistration(
            name=name, roles=("ios",), tags=(), preference=0,
            connection=RunHostConnection(f"https://{name}.invalid", "secret"), scope="user"), scope="user")
    monkeypatch.setenv("MSHIP_RUN_HOST_IOS_URL", "https://override.invalid")
    monkeypatch.setenv("MSHIP_RUN_HOST_IOS_TOKEN", "override-secret")

    with pytest.raises(RunHostError, match="ambiguous") as error:
        resolve_run_host("ios", repo=None, config=_config("ios"), store=store)
    assert "override-secret" not in str(error.value)


def test_migration_keeps_an_exact_private_legacy_backup(tmp_path: Path):
    state = tmp_path / "project"
    path = state / "run-hosts.yaml"
    path.parent.mkdir(parents=True)
    legacy = b"ios:\n  url: https://old.invalid\n  token: old-token\n"
    path.write_bytes(legacy)

    report = RunHostStore(state, user_config_dir=tmp_path / "user").migrate(
        scope="project", allowed_roles=("ios",), apply=True)

    assert report.backup_path is not None
    assert report.backup_path.read_bytes() == legacy
    assert report.backup_path.stat().st_mode & 0o777 == 0o600


def test_parallel_host_writes_do_not_lose_an_entry(tmp_path: Path):
    state = tmp_path / "project"

    def add(name: str):
        store = RunHostStore(state, user_config_dir=tmp_path / "user")
        store.set_host(HostRegistration(
            name=name, roles=(name,), tags=(), preference=0,
            connection=RunHostConnection(f"https://{name}.invalid", "private"),
            scope="project"), scope="project")

    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(add, ("one", "two")))

    assert set(RunHostStore(state, user_config_dir=tmp_path / "user").effective_hosts()) == {"one", "two"}


def test_interrupted_migration_leaves_the_active_legacy_file_intact(tmp_path: Path, monkeypatch):
    state = tmp_path / "project"
    path = state / "run-hosts.yaml"
    path.parent.mkdir(parents=True)
    legacy = b"ios:\n  url: https://old.invalid\n  token: old-token\n"
    path.write_bytes(legacy)
    store = RunHostStore(state, user_config_dir=tmp_path / "user")

    def interrupt(*_args):
        raise OSError("interrupted")

    monkeypatch.setattr(store, "_atomic_write", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        store.migrate(scope="project", allowed_roles=("ios",), apply=True)
    assert path.read_bytes() == legacy
    assert (state / "run-hosts.yaml.legacy.bak").read_bytes() == legacy

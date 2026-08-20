"""Registry model + flock'd store (#472 Task 1). The two-hosts test is the
behavioral pin for "no exclusive cross-host ownership": arbitration belongs to
#473's claims (see WorkspaceEntry docstring), not to registry state."""

import multiprocessing
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.daemon.paths import daemon_config_path, registry_path
from mship.core.daemon.registry import (
    DaemonConfig,
    RegistryState,
    RegistryStore,
    RepoInfo,
    WorkspaceEntry,
    load_daemon_config,
    mint_workspace_id,
    save_daemon_config,
)

NOW = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)


def _entry(id="ws-x", path="/w/a", **kw):
    defaults = dict(
        id=id,
        name="a",
        path=path,
        config_path=f"{path}/mothership.yaml",
        first_seen=NOW,
        last_seen=NOW,
    )
    defaults.update(kw)
    return WorkspaceEntry(**defaults)


def test_paths_are_pure(tmp_path: Path):
    assert (
        daemon_config_path(tmp_path)
        == tmp_path / ".mothership" / "daemon" / "config.yaml"
    )
    assert (
        registry_path(tmp_path)
        == tmp_path / ".mothership" / "daemon" / "workspaces.json"
    )


def test_entry_roundtrip_and_skew_tolerance(tmp_path: Path):
    store = RegistryStore(registry_path(tmp_path))
    e = _entry(
        repos=[RepoInfo(name="app", path="app", git_root="root")],
        runner={"enabled": True},
    )
    store.mutate(lambda s: s.entries.append(e))
    loaded = store.load()
    assert loaded.entries[0].id == "ws-x"
    assert loaded.entries[0].repos[0].git_root == "root"
    assert loaded.entries[0].runner == {"enabled": True}
    # forward-skew: unknown fields ignored
    raw = (
        registry_path(tmp_path)
        .read_text()
        .replace('"entries":', '"future_field": 1, "entries":')
    )
    registry_path(tmp_path).write_text(raw)
    assert store.load().entries[0].id == "ws-x"


def test_id_is_minted_never_dirname():
    a = mint_workspace_id(NOW)
    b = mint_workspace_id(NOW)
    assert a != b
    assert a.startswith("ws-20260817")
    assert "/" not in a


def _mutate_worker(path_str: str, worker: int, rounds: int) -> None:
    store = RegistryStore(Path(path_str))
    for j in range(rounds):

        def add(s, worker=worker, j=j):
            s.entries.append(_entry(id=f"ws-{worker}-{j}", path=f"/w/{worker}/{j}"))

        store.mutate(add)


def test_mutate_no_lost_updates(tmp_path: Path):
    path = registry_path(tmp_path)
    workers, rounds = 6, 4
    procs = [
        multiprocessing.Process(target=_mutate_worker, args=(str(path), w, rounds))
        for w in range(workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    ids = {e.id for e in RegistryStore(path).load().entries}
    assert ids == {f"ws-{w}-{j}" for w in range(workers) for j in range(rounds)}


def test_two_hosts_independent_entries(tmp_path: Path):
    """Same workspace dir in two registries (two homes): two independent
    entries, mutations invisible to each other, no ownership semantics."""
    ws = tmp_path / "shared-ws"
    ws.mkdir()
    home_a, home_b = tmp_path / "host-a", tmp_path / "host-b"
    store_a = RegistryStore(registry_path(home_a))
    store_b = RegistryStore(registry_path(home_b))
    store_a.mutate(lambda s: s.entries.append(_entry(id="ws-a", path=str(ws))))
    store_b.mutate(lambda s: s.entries.append(_entry(id="ws-b", path=str(ws))))
    store_a.mutate(lambda s: setattr(s.entries[0], "ignored", True))
    assert store_a.load().entries[0].ignored is True
    assert store_b.load().entries[0].ignored is False
    assert store_a.load().entries[0].id != store_b.load().entries[0].id


def test_daemon_config_missing_file_scans_nothing(tmp_path: Path):
    cfg = load_daemon_config(tmp_path)
    assert cfg.scan_roots == []
    assert cfg.serve is None


def test_daemon_config_roundtrip(tmp_path: Path):
    save_daemon_config(
        tmp_path,
        DaemonConfig(scan_roots=["/src"], serve={"host": "127.0.0.1", "port": 47190}),
    )
    cfg = load_daemon_config(tmp_path)
    assert cfg.scan_roots == ["/src"]
    assert cfg.serve == {"host": "127.0.0.1", "port": 47190}
    assert cfg.max_depth == 6


def test_daemon_config_relative_roots_rejected(tmp_path: Path):
    daemon_config_path(tmp_path).parent.mkdir(parents=True)
    daemon_config_path(tmp_path).write_text("scan_roots: ['src/relative']\n")
    with pytest.raises(ValueError, match="absolute"):
        load_daemon_config(tmp_path)


def test_daemon_config_malformed_yaml_is_a_value_error(tmp_path: Path):
    path = daemon_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("scan_roots: [\n")

    with pytest.raises(ValueError, match="invalid daemon config"):
        load_daemon_config(tmp_path)


def test_daemon_config_read_error_is_not_treated_as_absent(tmp_path: Path, monkeypatch):
    path = daemon_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    original = b"scan_roots: []\n"
    path.write_bytes(original)
    real_read_text = Path.read_text

    def fail_config_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_config_read)
    with pytest.raises(ValueError, match=str(path)):
        load_daemon_config(tmp_path)

    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "serve",
    [
        {"host": "127.0.0.1"},
        {"port": 47190},
        {"host": "", "port": 47190},
        {"host": "127.0.0.1", "port": 0},
        {"host": "127.0.0.1", "port": 65536},
    ],
)
def test_daemon_config_rejects_invalid_serve_bind(serve):
    with pytest.raises(ValueError, match="serve"):
        DaemonConfig(serve=serve)


def test_daemon_config_missing_relay_block_is_tunnel_disabled(tmp_path: Path):
    """No `relay:` is the ordinary LAN/tailnet host, not a misconfiguration."""
    assert load_daemon_config(tmp_path).relay is None


def test_daemon_config_relay_roundtrip(tmp_path: Path):
    save_daemon_config(
        tmp_path,
        DaemonConfig(relay={"host": "relay.example.com", "ssh_port": 2222}),
    )
    assert load_daemon_config(tmp_path).relay == {
        "host": "relay.example.com",
        "ssh_port": 2222,
    }


@pytest.mark.parametrize(
    "relay",
    [
        {},
        {"ssh_port": 2222},
        {"host": ""},
        {"host": 123},
        {"user": "mship"},
        "relay.example.com",
    ],
)
def test_daemon_config_rejects_relay_block_without_host(relay):
    """A present-but-hostless block is a typo, never a quiet "tunnel off": the
    host it was meant to reach would silently never be dialed."""
    with pytest.raises(ValueError, match="relay"):
        DaemonConfig(relay=relay)


@pytest.mark.parametrize("port", [0, 65536, True, "2222"])
def test_daemon_config_rejects_invalid_relay_ssh_port(port):
    with pytest.raises(ValueError, match="relay.ssh_port"):
        DaemonConfig(relay={"host": "relay.example.com", "ssh_port": port})


def test_daemon_config_rejects_negative_max_depth():
    with pytest.raises(ValueError, match="max_depth"):
        DaemonConfig(max_depth=-1)


def test_registry_lock_boundary_enforces_owner_only_modes(tmp_path: Path):
    import stat

    path = registry_path(tmp_path)
    RegistryStore(path).load()

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.with_name(path.name + ".lock").stat().st_mode) == 0o600


def test_corrupt_registry_loads_empty(tmp_path: Path):
    p = registry_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("{broken")
    assert RegistryStore(p).load() == RegistryState()


def test_registry_read_error_fails_load_mutate_and_reconcile_without_overwrite(
    tmp_path, monkeypatch
):
    from mship.core.daemon.registry import RegistryReadError, reconcile

    path = registry_path(tmp_path)
    store = RegistryStore(path)
    store.mutate(lambda state: state.entries.append(_entry(id="preserved")))
    previous = path.read_bytes()
    real_read_text = Path.read_text
    mutation_calls = 0

    def fail_registry_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("permission denied")
        return real_read_text(self, *args, **kwargs)

    def mutation(state):
        nonlocal mutation_calls
        mutation_calls += 1
        state.entries.clear()

    monkeypatch.setattr(Path, "read_text", fail_registry_read)
    operations = (
        store.load,
        lambda: store.mutate(mutation),
        lambda: reconcile(store, [], NOW),
    )
    for operation in operations:
        with pytest.raises(RegistryReadError, match=str(path)):
            operation()

    assert mutation_calls == 0
    assert path.read_bytes() == previous


def test_daemon_config_save_is_atomically_visible(tmp_path, monkeypatch):
    previous = DaemonConfig(scan_roots=["/previous"])
    replacement = DaemonConfig(scan_roots=["/replacement"])
    save_daemon_config(tmp_path, previous)
    path = daemon_config_path(tmp_path)
    replace_started = threading.Event()
    allow_replace = threading.Event()
    errors = []
    real_replace = os.replace

    def delayed_replace(source, destination):
        replace_started.set()
        if not allow_replace.wait(timeout=1):
            raise TimeoutError("test did not release config replace")
        real_replace(source, destination)

    def save_replacement():
        try:
            save_daemon_config(tmp_path, replacement)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(os, "replace", delayed_replace)
    worker = threading.Thread(target=save_replacement, daemon=True)
    worker.start()
    assert replace_started.wait(timeout=1)
    assert load_daemon_config(tmp_path) == previous
    allow_replace.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert errors == []
    assert load_daemon_config(tmp_path) == replacement
    assert path.stat().st_mode & 0o777 == 0o600


def test_daemon_config_replace_failure_preserves_previous_bytes(tmp_path, monkeypatch):
    previous = DaemonConfig(scan_roots=["/previous"])
    save_daemon_config(tmp_path, previous)
    path = daemon_config_path(tmp_path)
    previous_bytes = path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        save_daemon_config(tmp_path, DaemonConfig(scan_roots=["/replacement"]))

    assert path.read_bytes() == previous_bytes
    assert list(path.parent.glob(path.name + ".*")) == []

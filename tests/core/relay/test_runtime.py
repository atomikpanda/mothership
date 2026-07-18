import json

from mship.core.relay.runtime import (
    RelayRuntimeRecord,
    clear_runtime_record,
    live_runtime_record,
    read_runtime_record,
    write_runtime_record,
)


def test_write_then_read_roundtrips(tmp_path):
    rec = RelayRuntimeRecord(
        host="relay.example.com",
        pid=4321,
        subdomain="abc-def",
        url="https://abc-def.relay.example.com",
        workspace="ws",
        ssh_port=2222,
        user="tunnel",
    )
    write_runtime_record(tmp_path, rec)
    assert read_runtime_record(tmp_path) == rec


def test_write_is_mode_0600(tmp_path):
    write_runtime_record(tmp_path, RelayRuntimeRecord(host="h", pid=1))
    path = tmp_path / ".mothership" / "relay-runtime.json"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_written_file_is_json_with_host_and_pid(tmp_path):
    write_runtime_record(tmp_path, RelayRuntimeRecord(host="relay.h", pid=99))
    raw = json.loads((tmp_path / ".mothership" / "relay-runtime.json").read_text())
    assert raw["host"] == "relay.h"
    assert raw["pid"] == 99


def test_read_absent_returns_none(tmp_path):
    assert read_runtime_record(tmp_path) is None


def test_read_corrupt_json_returns_none(tmp_path):
    d = tmp_path / ".mothership"
    d.mkdir()
    (d / "relay-runtime.json").write_text("{not json")
    assert read_runtime_record(tmp_path) is None


def test_read_missing_required_keys_returns_none(tmp_path):
    d = tmp_path / ".mothership"
    d.mkdir()
    (d / "relay-runtime.json").write_text(json.dumps({"host": "h"}))  # no pid
    assert read_runtime_record(tmp_path) is None


def test_clear_removes_record_idempotently(tmp_path):
    write_runtime_record(tmp_path, RelayRuntimeRecord(host="h", pid=1))
    clear_runtime_record(tmp_path)
    assert read_runtime_record(tmp_path) is None
    clear_runtime_record(tmp_path)  # no error when already gone


def test_live_record_returned_when_pid_alive(tmp_path):
    write_runtime_record(tmp_path, RelayRuntimeRecord(host="h", pid=1234))
    got = live_runtime_record(tmp_path, pid_alive=lambda pid: True)
    assert got is not None and got.host == "h"


def test_stale_record_ignored_when_pid_dead(tmp_path):
    write_runtime_record(tmp_path, RelayRuntimeRecord(host="h", pid=1234))
    assert live_runtime_record(tmp_path, pid_alive=lambda pid: False) is None


def test_live_record_none_when_absent(tmp_path):
    assert live_runtime_record(tmp_path, pid_alive=lambda pid: True) is None


def test_live_record_checks_the_records_pid(tmp_path):
    write_runtime_record(tmp_path, RelayRuntimeRecord(host="h", pid=777))
    seen = {}

    def fake_alive(pid):
        seen["pid"] = pid
        return True

    live_runtime_record(tmp_path, pid_alive=fake_alive)
    assert seen["pid"] == 777


def test_default_pid_alive_true_for_current_process(tmp_path):
    import os

    write_runtime_record(tmp_path, RelayRuntimeRecord(host="h", pid=os.getpid()))
    assert live_runtime_record(tmp_path) is not None  # real _pid_alive on this pid

import json

from mship.core.relay.runtime import (
    RelayRuntimeRecord,
    clear_runtime_record,
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

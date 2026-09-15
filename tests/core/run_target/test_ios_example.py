"""Behavioral contract coverage for the opt-in native iOS simulator example."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_EXAMPLE = _ROOT / "examples" / "run-targets" / "ios" / "backend.py"
_FIXTURES = Path(__file__).with_name("fixtures") / "ios"
_UUID = "AAAAAAAA-1111-2222-3333-AAAAAAAAAAAA"
_RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-18-2"
_DEVICE_TYPE = "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro"


def _backend() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ios_example_backend", _EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inventory() -> dict[str, object]:
    return json.loads((_FIXTURES / "simctl-inventory-redacted.json").read_text())


def _bindings() -> dict[str, object]:
    return {
        "paths": {
            "xcrun": "/Applications/Xcode.app/Contents/Developer/usr/bin/xcrun",
            "bundle_id": "com.example.product",
        },
        "aliases": {"qa_phone": _UUID},
    }


def _request(operation: str = "run") -> dict[str, object]:
    return {
        "protocol_version": 1,
        "backend": "ios-simctl",
        "backend_revision": "a" * 40,
        "profile": "ios-qa",
        "profile_revision": "b" * 64,
        "task": "task-1",
        "repo": "app",
        "operation": operation,
        "options": {},
        "target_alias": "qa_phone",
    }


def _identity(path: object) -> tuple[str, int, int]:
    assert isinstance(path, str)
    return (path, 42, 99)


def test_discovery_derives_numeric_runtime_and_private_identity_from_simctl(
    monkeypatch: pytest.MonkeyPatch,
):
    backend, captured = _backend(), {}
    monkeypatch.setattr(backend, "_inventory", lambda _xcrun: _inventory())
    monkeypatch.setattr(backend, "_path_identity", _identity)
    monkeypatch.setattr(backend, "_app", lambda *_args: None)
    monkeypatch.setattr(
        backend,
        "emit_inventory",
        lambda _r, c, rank_schema=(), errors=(): captured.update(
            candidates=list(c), rank_schema=tuple(rank_schema), errors=tuple(errors)
        ),
    )

    backend.discover(_request(), _bindings())

    simulator = captured["candidates"][0]
    assert captured["rank_schema"] == ("ios_major", "ios_minor", "ios_patch")
    assert simulator["rank"] == [18, 2, 0]
    assert simulator["aliases"] == ["qa_phone"]
    assert simulator["binding"]["ios"]["uuid"] == _UUID
    assert simulator["binding"]["ios"]["device_type"] == _DEVICE_TYPE
    assert simulator["binding"]["ios"]["instance_fingerprint"]
    assert "binary_provenance" not in simulator["binding"]


def test_discovery_requires_data_path_identity_before_advertising_operations(
    monkeypatch: pytest.MonkeyPatch,
):
    backend, captured = _backend(), {}
    monkeypatch.setattr(backend, "_inventory", lambda _xcrun: _inventory())
    monkeypatch.setattr(backend, "_path_identity", lambda _path: None)
    monkeypatch.setattr(backend, "_app", lambda *_args: None)
    monkeypatch.setattr(
        backend,
        "emit_inventory",
        lambda _r, c, rank_schema=(), errors=(): captured.update(candidates=list(c)),
    )

    backend.discover(_request(), _bindings())

    simulator = captured["candidates"][0]
    assert simulator["ready"] is False
    assert simulator["reason"] == "identity-unknown"
    assert simulator["capabilities"] == []


def test_discovery_does_not_advertise_a_simulator_without_configured_app(
    monkeypatch: pytest.MonkeyPatch,
):
    backend, captured = _backend(), {}
    monkeypatch.setattr(backend, "_inventory", lambda _xcrun: _inventory())
    monkeypatch.setattr(backend, "_path_identity", _identity)
    monkeypatch.setattr(
        backend,
        "_app",
        lambda *_args: (_ for _ in ()).throw(
            backend.BackendError(
                "target-unavailable", "Configured iOS target operation failed"
            )
        ),
    )
    monkeypatch.setattr(
        backend,
        "emit_inventory",
        lambda _r, c, rank_schema=(), errors=(): captured.update(candidates=list(c)),
    )

    backend.discover(_request(), _bindings())

    simulator = captured["candidates"][0]
    assert simulator["reason"] == "app-unavailable"
    assert simulator["ready"] is False
    assert simulator["capabilities"] == []


def test_dynamic_revalidation_rejects_changed_device_type(
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _backend()
    monkeypatch.setattr(backend, "_path_identity", _identity)
    device = _inventory()["devices"][_RUNTIME][0]
    sealed = backend._binding(_UUID, _RUNTIME, device, "com.example.product")
    changed = deepcopy(device)
    changed["deviceTypeIdentifier"] = "com.apple.CoreSimulator.SimDeviceType.iPhone-15"

    assert sealed is not None
    assert backend._binding(_UUID, _RUNTIME, changed, "com.example.product") != sealed


def test_lifetime_refuses_preexisting_foreign_app_before_launch(
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _backend()
    monkeypatch.setattr(
        backend,
        "_run",
        lambda *_args, **_kwargs: b"123\tcom.example.product",
    )

    with pytest.raises(backend.BackendError, match="already running"):
        backend._assert_app_absent(
            "/opt/xcrun", {"uuid": _UUID, "bundle_id": "com.example.product"}
        )


def test_launch_acknowledges_exact_bundle_and_pid(monkeypatch: pytest.MonkeyPatch):
    backend = _backend()
    monkeypatch.setattr(
        backend, "_run", lambda *_args: b"com.example.product: 731\n"
    )
    assert backend._launch_pid(
        "/opt/xcrun", {"uuid": _UUID, "bundle_id": "com.example.product"}
    ) == 731


@pytest.mark.parametrize(
    "receipt",
    [
        b"com.example.other: 731\n",
        b"731\n",
        b"com.example.product: 0\n",
        b"com.example.product: 2147483648\n",
        b"com.example.product: 731\nunexpected\n",
    ],
)
def test_launch_rejects_unacknowledged_identity(
    monkeypatch: pytest.MonkeyPatch, receipt: bytes
):
    backend = _backend()
    monkeypatch.setattr(backend, "_run", lambda *_args: receipt)
    with pytest.raises(backend.BackendError, match="identity"):
        backend._launch_pid(
            "/opt/xcrun", {"uuid": _UUID, "bundle_id": "com.example.product"}
        )


def test_run_lifetime_terminates_only_the_verified_owned_launch_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    backend = _backend()
    terminated: list[tuple[str, ...]] = []
    handlers: dict[int, object] = {}
    private = tmp_path / "owner"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    owner = backend.OwnerContext(
        task="task-1",
        repo="app",
        owner_ref="a" * 24,
        generation="b" * 24,
        source_revision="c" * 40,
        workspace_root=tmp_path,
        worktree=tmp_path,
        private_root=private,
        socket_path=tmp_path / "owner.sock",
        secret="d" * 24,
    )
    owner_path = private / "owner-context.json"
    owner_path.write_text(json.dumps(owner.to_private_dict()))
    owner_path.chmod(0o600)
    monkeypatch.setenv("MSHIP_OWNER_CONTEXT_FILE", str(owner_path))
    monkeypatch.setenv("MSHIP_TASK", "task-1")
    monkeypatch.setenv("MSHIP_REPO", "app")
    monkeypatch.setenv("MSHIP_SOURCE_REVISION", "c" * 40)

    class Lease:
        def close(self) -> None:
            return None

    monkeypatch.setattr(backend, "acquire_app_lease", lambda *_args: Lease())
    monkeypatch.setattr(backend, "_assert_app_absent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backend, "_launch_pid", lambda *_args: 731)
    monkeypatch.setattr(backend, "_is_owned_launch", lambda *_args: True)
    monkeypatch.setattr(
        backend,
        "signal",
        type(
            "Signals",
            (),
            {
                "SIGINT": 2,
                "SIGTERM": 15,
                "signal": staticmethod(
                    lambda signum, handler: handlers.setdefault(signum, handler)
                ),
            },
        ),
    )
    monkeypatch.setattr(
        backend,
        "time",
        type(
            "Clock",
            (),
            {"sleep": staticmethod(lambda _seconds: handlers[2](2, None))},
        ),
    )
    monkeypatch.setattr(
        backend,
        "_run",
        lambda argv, **_kwargs: terminated.append(tuple(argv)) or b"",
    )

    backend._run_lifetime(
        "/opt/xcrun", {"uuid": _UUID, "bundle_id": "com.example.product"}
    )

    assert json.loads((private / "domain-cleanup.json").read_text()) == {
        "version": 1,
        "task": "task-1",
        "repo": "app",
        "owner_ref": "a" * 24,
        "generation": "b" * 24,
        "cleanup_known": True,
    }
    assert terminated == [
        ("/opt/xcrun", "simctl", "terminate", _UUID, "com.example.product")
    ]


def test_usb_tool_unavailable_is_distinct_and_never_advertises_operations(
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _backend()
    monkeypatch.setattr(
        backend,
        "_run",
        lambda _argv: (_ for _ in ()).throw(
            backend.BackendError("tool-unavailable", "missing")
        ),
    )

    candidate = backend._usb(("/opt/tools/native-ios-devices", "list", "--json"))

    assert candidate["tags"] == ["ios", "usb"]
    assert candidate["reason"] == "usb-tool-unavailable"
    assert candidate["ready"] is False and candidate["capabilities"] == []


def test_native_usb_fixture_remains_inventory_only(monkeypatch: pytest.MonkeyPatch):
    backend = _backend()
    monkeypatch.setattr(
        backend,
        "_run",
        lambda _argv: (_FIXTURES / "native-usb-inventory-redacted.json").read_bytes(),
    )

    candidate = backend._usb(("/opt/tools/native-ios-devices", "list", "--json"))

    assert candidate["reason"] == "usb-operations-unavailable"
    assert candidate["capabilities"] == []


def test_capture_uses_exact_selected_uuid_and_existing_artifact_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    backend, calls = _backend(), []
    sealed = {
        "uuid": _UUID,
        "runtime": _RUNTIME,
        "device_type": _DEVICE_TYPE,
        "data_path": "/private/redacted/simulator",
        "instance_fingerprint": "a" * 64,
        "bundle_id": "com.example.product",
    }
    monkeypatch.setenv("MSHIP_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setenv("MSHIP_CAPTURE_KINDS", "image")
    monkeypatch.setenv("MSHIP_CAPTURE_PLATFORM", "ios")
    monkeypatch.setattr(backend, "_selected", lambda _r, _b: ("/opt/xcrun", sealed))
    monkeypatch.setattr(backend, "_app", lambda *_args: None)

    def screenshot(argv, *, timeout=None):
        calls.append(tuple(argv))
        Path(argv[-1]).write_bytes(b"\x89PNG\r\n\x1a\nfixture")

    monkeypatch.setattr(backend, "_foreground", screenshot)

    backend.capture(_request("capture"), _bindings())

    assert calls == [
        (
            "/opt/xcrun",
            "simctl",
            "io",
            _UUID,
            "screenshot",
            str(tmp_path / "screen.png"),
        )
    ]


def test_capture_rejects_layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    backend = _backend()
    monkeypatch.setenv("MSHIP_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setenv("MSHIP_CAPTURE_KINDS", "image,layout")
    with pytest.raises(backend.BackendError, match="image only"):
        backend._capture_directory()

"""Behavioral contracts for the configured Playwright browser example."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_EXAMPLE = _ROOT / "examples" / "run-targets" / "browser" / "backend.py"
_FIXTURES = Path(__file__).with_name("fixtures") / "browser"


def _backend() -> ModuleType:
    spec = importlib.util.spec_from_file_location("browser_example_backend", _EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _request(operation: str = "run") -> dict[str, object]:
    return {
        "protocol_version": 1,
        "backend": "browser-playwright",
        "backend_revision": "a" * 40,
        "profile": "browser-qa",
        "profile_revision": "b" * 64,
        "task": "task-1",
        "repo": "web",
        "operation": operation,
        "options": {},
        "target_alias": "qa_chromium",
    }


def _records() -> list[dict[str, object]]:
    return json.loads((_FIXTURES / "managed-instances-redacted.json").read_text())


def _probe() -> dict[str, object]:
    return json.loads((_FIXTURES / "playwright-probe-redacted.json").read_text())


def _replaced_receipt() -> dict[str, object]:
    return json.loads((_FIXTURES / "replaced-receipt-redacted.json").read_text())


def _config() -> dict[str, str]:
    return {
        "node": "/bin/true",
        "playwright_module": "/private/playwright/index.mjs",
        "instances_dir": "/private/browser-instances",
    }


@contextmanager
def _stored_receipt(backend: ModuleType, tmp_path: Path):
    config = {**_config(), "instances_dir": str(tmp_path)}
    receipt_path = backend._receipt_path(config, "run-1")
    receipt_path.parent.mkdir(mode=0o700)
    with TemporaryDirectory(prefix="msb-") as directory:
        control_path = Path(directory).resolve() / "control.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(control_path))
            os.chmod(control_path, 0o600)
            receipt = {
                "version": 2,
                "run_id": "run-1",
                "instance_token": "browser_chromium_qa_0001",
                "record_fingerprint": "f" * 64,
                "engine": "chromium",
                "control_path": str(control_path),
                "control_token": base64.urlsafe_b64encode(b"t" * 32)
                .rstrip(b"=")
                .decode("ascii"),
                "page_url": "https://example.invalid/app",
                "page_marker": "mship-page-marker",
            }
            receipt_path.write_text(json.dumps(receipt))
            os.chmod(receipt_path, 0o600)
            yield config, receipt


def test_discovery_uses_only_the_read_only_probe_and_emits_engine_candidates(
    monkeypatch: pytest.MonkeyPatch,
):
    backend, captured, calls = _backend(), {}, []
    monkeypatch.setattr(backend, "_browser_config", lambda _bindings: _config())
    monkeypatch.setattr(
        backend,
        "_load_records",
        lambda _config: [backend._record(item) for item in _records()],
    )
    monkeypatch.setattr(
        backend,
        "_driver",
        lambda action, _config, **_kwargs: calls.append(action) or _probe(),
    )
    monkeypatch.setattr(
        backend,
        "emit_inventory",
        lambda _request, candidates, rank_schema=(), errors=(): captured.update(
            candidates=list(candidates),
            rank_schema=tuple(rank_schema),
            errors=tuple(errors),
        ),
    )

    backend.discover(
        _request(),
        {
            "paths": {"browser": {}},
            "aliases": {"browser": {"qa_chromium": "browser_chromium_qa_0001"}},
        },
    )

    assert calls == ["probe"]
    assert captured["rank_schema"] == ("engine_available", "engine_version")
    chromium = next(item for item in captured["candidates"] if item["aliases"])
    assert chromium["binding"]["platform"] == "browser"
    assert chromium["binding"]["engine"] == "chromium"
    assert chromium["binding"]["instance_token"] == "browser_chromium_qa_0001"
    assert "binary_provenance" not in chromium["binding"]
    unconfigured = next(
        item
        for item in captured["candidates"]
        if item["binding"]["engine"] == "firefox"
    )
    assert unconfigured["ready"] is False
    assert unconfigured["reason"] == "page-unconfigured"
    assert unconfigured["capabilities"] == []


def test_missing_engine_is_unready_and_unsupported_engine_record_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
):
    backend, captured = _backend(), {}
    records = _records()
    probe = _probe()
    probe["engines"]["webkit"]["ready"] = False
    monkeypatch.setattr(backend, "_browser_config", lambda _bindings: _config())
    monkeypatch.setattr(
        backend,
        "_load_records",
        lambda _config: [backend._record(item) for item in records],
    )
    monkeypatch.setattr(backend, "_driver", lambda *_args, **_kwargs: probe)
    monkeypatch.setattr(
        backend,
        "emit_inventory",
        lambda _request, candidates, rank_schema=(), errors=(): captured.update(
            candidates=list(candidates)
        ),
    )

    backend.discover(_request(), {"paths": {"browser": {}}, "aliases": {}})

    webkit = next(
        item for item in captured["candidates"] if item["binding"]["engine"] == "webkit"
    )
    assert webkit["ready"] is False
    assert webkit["reason"] == "engine-unavailable"
    assert webkit["capabilities"] == []
    with pytest.raises(backend.BackendError, match="unsupported browser engine"):
        backend._record({**records[0], "engine": "safari"})


def test_selected_context_rejects_a_replaced_managed_instance(
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _backend()
    record = backend._record(_records()[0])
    binding = backend._binding(record, _probe()["engines"]["chromium"], "f" * 64)
    receipt = _replaced_receipt()
    monkeypatch.setattr(backend, "_browser_config", lambda _bindings: _config())
    monkeypatch.setattr(
        backend,
        "load_context",
        lambda: {"run_id": "run-redacted", "private_binding": binding},
    )
    monkeypatch.setattr(backend, "_load_record", lambda _config, _token: record)
    monkeypatch.setattr(backend, "_read_receipt", lambda _config, _run_id: receipt)

    with pytest.raises(backend.BackendError, match="identity changed"):
        backend._selected(_request(), {"paths": {"browser": {}}})


@pytest.mark.parametrize("invalid", ("legacy", "insecure-socket", "bad-token"))
def test_receipt_requires_current_private_control_endpoint(
    tmp_path: Path, invalid: str
):
    backend = _backend()
    with _stored_receipt(backend, tmp_path) as (config, receipt):
        receipt_path = backend._receipt_path(config, "run-1")
        assert backend._read_receipt(config, "run-1") == receipt
        if invalid == "legacy":
            receipt["version"] = 1
            receipt_path.write_text(json.dumps(receipt))
            os.chmod(receipt_path, 0o600)
        elif invalid == "insecure-socket":
            os.chmod(receipt["control_path"], 0o644)
        else:
            receipt["control_token"] = "invalid"
            receipt_path.write_text(json.dumps(receipt))
            os.chmod(receipt_path, 0o600)
        with pytest.raises(backend.BackendError, match="receipt is invalid"):
            backend._read_receipt(config, "run-1")


def test_driver_shutdown_allows_graceful_cleanup_before_escalation():
    backend = _backend()
    driver = subprocess.Popen(
        (
            sys.executable,
            "-c",
            (
                "import signal, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "def stop(*_):\n"
                " try: child.wait(timeout=0.1); survived = False\n"
                " except subprocess.TimeoutExpired: survived = True\n"
                " child.terminate(); child.wait(timeout=1)\n"
                " print('cleanup' if survived else 'lost-child', flush=True)\n"
                " raise SystemExit(0 if survived else 1)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "print('ready', flush=True)\n"
                "while True: time.sleep(1)\n"
            ),
        ),
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert driver.stdout is not None
        assert driver.stdout.readline() == b"ready\n"
        backend._stop_owned_driver(driver)
        assert driver.stdout.readline() == b"cleanup\n"
        assert driver.returncode == 0
    finally:
        backend._stop_owned_driver(driver)


def test_observation_requires_the_receipt_bound_existing_page_marker(
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _backend()
    receipt = {
        "version": 2,
        "run_id": "run-1",
        "instance_token": "browser_chromium_qa_0001",
        "record_fingerprint": "f" * 64,
        "engine": "chromium",
        "control_path": "/private/browser-instances/runs/mship-browser/control.sock",
        "control_token": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "page_url": "https://example.invalid/app",
        "page_marker": "mship-page-marker",
    }
    monkeypatch.setattr(
        backend,
        "_driver",
        lambda _action, _config, **_kwargs: {
            "page_url": "https://example.invalid/other",
            "page_marker": "mship-page-marker",
        },
    )

    with pytest.raises(backend.BackendError, match="selected browser page changed"):
        backend._observe(receipt, _config())

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.config import Healthcheck
from mship.core.healthcheck import (
    HealthcheckRunner,
    HealthcheckResult,
    _parse_duration,
)
from mship.util.shell import ShellRunner, ShellResult


def test_parse_duration_seconds():
    assert _parse_duration("30s") == 30.0


def test_parse_duration_ms():
    assert _parse_duration("500ms") == 0.5


def test_parse_duration_minutes():
    assert _parse_duration("2m") == 120.0


def test_parse_duration_invalid():
    with pytest.raises(ValueError):
        _parse_duration("nope")


def test_sleep_probe_always_ready():
    runner = HealthcheckRunner(MagicMock(spec=ShellRunner))
    hc = Healthcheck(sleep="50ms")
    result = runner.wait(hc, Path("."))
    assert result.ready
    assert "slept" in result.message
    assert result.duration_s >= 0.04  # some slack


def test_tcp_probe_ready_when_port_open():
    # Start a local TCP server
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen(1)

    try:
        runner = HealthcheckRunner(MagicMock(spec=ShellRunner))
        hc = Healthcheck(tcp=f"127.0.0.1:{port}", timeout="2s", retry_interval="100ms")
        result = runner.wait(hc, Path("."))
        assert result.ready
        assert "ready after" in result.message
        assert "tcp" in result.message
    finally:
        server.close()


def test_tcp_probe_timeout_when_port_closed():
    runner = HealthcheckRunner(MagicMock(spec=ShellRunner))
    # Use a port nothing is listening on
    hc = Healthcheck(tcp="127.0.0.1:1", timeout="300ms", retry_interval="100ms")
    result = runner.wait(hc, Path("."))
    assert not result.ready
    assert "timeout" in result.message


def test_http_probe_ready_when_server_responds():
    class OKHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args, **kwargs):
            pass

    server = HTTPServer(("127.0.0.1", 0), OKHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        runner = HealthcheckRunner(MagicMock(spec=ShellRunner))
        hc = Healthcheck(
            http=f"http://127.0.0.1:{port}/", timeout="2s", retry_interval="100ms"
        )
        result = runner.wait(hc, Path("."))
        assert result.ready
        assert "http" in result.message
    finally:
        server.shutdown()


def test_http_probe_timeout_when_no_server():
    runner = HealthcheckRunner(MagicMock(spec=ShellRunner))
    hc = Healthcheck(
        http="http://127.0.0.1:1/nowhere",
        timeout="300ms",
        retry_interval="100ms",
    )
    result = runner.wait(hc, Path("."))
    assert not result.ready
    assert "timeout" in result.message


def test_task_probe_ready_when_exit_0():
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    runner = HealthcheckRunner(mock_shell)
    hc = Healthcheck(task="wait-ready", timeout="2s", retry_interval="100ms")
    result = runner.wait(hc, Path("."))
    assert result.ready
    assert "task" in result.message


def test_task_probe_timeout_when_exit_nonzero():
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=1, stdout="", stderr="not ready")
    runner = HealthcheckRunner(mock_shell)
    hc = Healthcheck(task="check", timeout="300ms", retry_interval="100ms")
    result = runner.wait(hc, Path("."))
    assert not result.ready
    assert "not ready" in result.message or "timeout" in result.message

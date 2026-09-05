"""Real macOS launchd lifecycle, isolated from the operator's daemon and home."""

import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from mship.core.daemon import supervisor as supervisor_mod
from mship.core.daemon import units as units_mod
from mship.core.daemon.control import probe_control_socket
from mship.core.daemon.paths import daemon_socket_path

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="requires macOS launchd"
)


def _wait_for(condition, supervisor):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(0.1)
    pytest.fail(
        f"launchd condition timed out: {supervisor.query()}\n"
        + "\n".join(supervisor.logs_tail(30))
    )


@pytest.fixture
def launchd_daemon(monkeypatch):
    label = f"com.mothership.test.{uuid.uuid4().hex}"
    monkeypatch.setattr(supervisor_mod, "LAUNCHD_LABEL", label)
    monkeypatch.setattr(units_mod, "LAUNCHD_LABEL", label)
    # Darwin's Unix socket path limit is short; pytest's default path can exceed it.
    with tempfile.TemporaryDirectory(prefix="mship-", dir="/tmp") as directory:
        home = Path(directory)
        env = {"HOME": str(home), "XDG_RUNTIME_DIR": str(home / "runtime")}
        socket = daemon_socket_path(env, home)
        supervisor = supervisor_mod.LaunchdSupervisor(home=home)
        assert supervisor.available(), (
            "macOS CI must provide a reachable user launchd domain"
        )
        argv = [
            "/usr/bin/env",
            "-i",
            f"HOME={home}",
            f"XDG_RUNTIME_DIR={env['XDG_RUNTIME_DIR']}",
            "PATH=/usr/bin:/bin",
            sys.executable,
            "-m",
            "mship.core.daemon",
        ]
        target = f"user/{os.getuid()}/{label}"
        try:
            yield supervisor, socket, argv, target
        finally:
            subprocess.run(
                ["launchctl", "bootout", "--wait", target],
                capture_output=True,
                timeout=15,
            )
            _wait_for(lambda: probe_control_socket(socket) is None, supervisor)


def _healthy(socket, previous_pid=None):
    health = probe_control_socket(socket)
    return health if health and health["pid"] != previous_pid else None


def test_launchd_runs_stops_restarts_and_recovers_daemon(launchd_daemon):
    supervisor, socket, argv, target = launchd_daemon
    supervisor.install(argv)
    first = _wait_for(lambda: _healthy(socket), supervisor)
    assert supervisor.query().state == "active"
    print(f"installed: healthy pid={first['pid']}")

    supervisor.start()
    assert _healthy(socket)["pid"] == first["pid"]

    supervisor.restart()
    restarted = _wait_for(lambda: _healthy(socket, first["pid"]), supervisor)
    print(f"restarted: healthy pid={restarted['pid']}")

    supervisor.stop()
    _wait_for(lambda: probe_control_socket(socket) is None, supervisor)
    assert supervisor.query().state == "absent"

    supervisor.start()
    started = _wait_for(lambda: _healthy(socket, restarted["pid"]), supervisor)
    print(f"started after stop: healthy pid={started['pid']}")

    supervisor.install(argv)
    reinstalled = _wait_for(lambda: _healthy(socket, started["pid"]), supervisor)
    print(f"reinstalled: healthy pid={reinstalled['pid']}")

    subprocess.run(["launchctl", "kill", "SIGKILL", target], check=True, timeout=15)
    recovered = _wait_for(lambda: _healthy(socket, reinstalled["pid"]), supervisor)
    assert supervisor.query().state == "active"
    print(f"recovered after crash: healthy pid={recovered['pid']}")


def test_launchd_start_recovers_loaded_daemon_after_clean_exit(launchd_daemon):
    supervisor, socket, argv, target = launchd_daemon
    supervisor.install(argv)
    first = _wait_for(lambda: _healthy(socket), supervisor)
    # A graceful exit leaves the service loaded, but SuccessfulExit=false does
    # not respawn it. start must kickstart that service, not bootstrap it twice.
    subprocess.run(["launchctl", "kill", "SIGTERM", target], check=True, timeout=15)
    _wait_for(lambda: probe_control_socket(socket) is None, supervisor)
    _wait_for(lambda: supervisor.query().state != "active", supervisor)
    supervisor.start()
    recovered = _wait_for(lambda: _healthy(socket, first["pid"]), supervisor)
    assert supervisor.query().state == "active"
    print(f"recovered after clean exit: healthy pid={recovered['pid']}")

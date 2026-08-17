"""Supervisor adapter: every OS-supervisor invocation goes through this one
injectable boundary, tested with a recorder fake for run_cmd."""
import subprocess
from pathlib import Path

import pytest

from mship.core.daemon.supervisor import (
    DaemonSupervisorError,
    LaunchdSupervisor,
    SystemdUserSupervisor,
    pick_supervisor,
)


class Recorder:
    """Fake subprocess.run: records argv, returns scripted results."""

    def __init__(self, responses=None):
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        key = " ".join(argv)
        for pattern, resp in self.responses.items():
            if pattern in key:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _fail(stdout="", stderr="", code=1):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


def _linux(tmp_path, responses=None):
    rec = Recorder(responses)
    sup = SystemdUserSupervisor(home=tmp_path, user="bailey", run_cmd=rec)
    return sup, rec


def test_linux_install_order_and_linger_verify(tmp_path):
    sup, rec = _linux(
        tmp_path, {"show-user": _ok("Linger=yes\n"), "is-system-running": _ok("running\n")}
    )
    sup.install(["/venv/bin/mshipd"])
    unit = tmp_path / ".config" / "systemd" / "user" / "mship-daemon.service"
    assert unit.is_file()
    cmds = [" ".join(c) for c in rec.calls]
    reload_i = next(i for i, c in enumerate(cmds) if "daemon-reload" in c)
    enable_i = next(i for i, c in enumerate(cmds) if " enable " in f" {c} ")
    linger_i = next(i for i, c in enumerate(cmds) if "enable-linger" in c)
    verify_i = next(i for i, c in enumerate(cmds) if "show-user" in c)
    assert reload_i < enable_i < linger_i < verify_i


def test_linux_install_fails_loudly_when_linger_does_not_stick(tmp_path):
    sup, _ = _linux(tmp_path, {"show-user": _ok("Linger=no\n")})
    with pytest.raises(DaemonSupervisorError, match="[Ll]inger"):
        sup.install(["/venv/bin/mshipd"])


def test_linux_lifecycle_commands(tmp_path):
    sup, rec = _linux(tmp_path)
    sup.start()
    sup.stop()
    sup.restart()
    assert [c[2] for c in rec.calls] == ["start", "stop", "restart"]
    assert all(c[:2] == ["systemctl", "--user"] for c in rec.calls)


def test_linux_query_states(tmp_path):
    sup, _ = _linux(tmp_path, {"show mship-daemon": _ok("ActiveState=active\nSubState=running\n")})
    assert sup.query().state == "active"
    sup2, _ = _linux(tmp_path, {"show mship-daemon": _ok("ActiveState=failed\nSubState=failed\n")})
    assert sup2.query().state == "failed"
    sup3, _ = _linux(tmp_path, {"show mship-daemon": _ok("ActiveState=inactive\nSubState=dead\n")})
    assert sup3.query().state == "absent"
    sup4, _ = _linux(tmp_path, {"show mship-daemon": _fail(stderr="Failed to connect to bus")})
    assert sup4.query().state == "unreachable"
    sup5, _ = _linux(tmp_path, {"show mship-daemon": _ok("garbage")})
    assert sup5.query().state == "absent"  # parse failure → absent-with-warning, never raise


def test_linger_state_parses(tmp_path):
    sup, _ = _linux(tmp_path, {"show-user": _ok("Linger=yes\n")})
    assert sup.linger_state() == "yes"
    sup2, _ = _linux(tmp_path, {"show-user": _ok("Linger=no\n")})
    assert sup2.linger_state() == "no"
    sup3, _ = _linux(tmp_path, {"show-user": _fail(stderr="boom")})
    assert sup3.linger_state() == "unknown"


def test_available_probes_user_manager_not_binary(tmp_path):
    # systemctl exists but every --user call dies with a bus error → unavailable.
    sup, _ = _linux(
        tmp_path, {"is-system-running": _fail(stderr="Failed to connect to bus: No medium found")}
    )
    assert sup.available() is False
    # Even a degraded manager reply proves reachability.
    sup2, _ = _linux(tmp_path, {"is-system-running": _fail(stdout="degraded\n")})
    assert sup2.available() is True
    sup3, _ = _linux(tmp_path, {"is-system-running": _ok("running\n")})
    assert sup3.available() is True


def test_available_true_but_reload_bus_error_names_fallback(tmp_path):
    sup, _ = _linux(
        tmp_path,
        {
            "is-system-running": _ok("running\n"),
            "show-user": _ok("Linger=yes\n"),
            "daemon-reload": _fail(stderr="Failed to connect to bus"),
        },
    )
    with pytest.raises(DaemonSupervisorError, match="mship daemon run"):
        sup.install(["/venv/bin/mshipd"])


def _mac(tmp_path, responses=None):
    rec = Recorder(responses)
    sup = LaunchdSupervisor(home=tmp_path, uid=501, run_cmd=rec)
    return sup, rec


def test_macos_install_uses_user_domain_not_gui(tmp_path):
    """gui/<uid> bootstrap fails over SSH with no GUI session ('Bootstrap
    failed: 5: Input/output error') — exactly the headless provisioning
    scenario #469/#470 describe."""
    sup, rec = _mac(tmp_path)
    sup.install(["/venv/bin/mshipd"])
    plist = tmp_path / "Library" / "LaunchAgents" / "com.mothership.daemon.plist"
    assert plist.is_file()
    cmds = [" ".join(c) for c in rec.calls]
    assert any("bootstrap user/501" in c for c in cmds)
    assert not any("gui/" in c for c in cmds)


def test_macos_bootstrap_failure_is_daemon_error(tmp_path):
    sup, _ = _mac(tmp_path, {"bootstrap": _fail(stderr="Bootstrap failed: 5: Input/output error")})
    with pytest.raises(DaemonSupervisorError, match="[Bb]ootstrap"):
        sup.install(["/venv/bin/mshipd"])


def test_macos_lifecycle_commands(tmp_path):
    sup, rec = _mac(tmp_path)
    sup.start()
    sup.stop()
    sup.restart()
    cmds = [" ".join(c) for c in rec.calls]
    assert any("kickstart user/501/com.mothership.daemon" in c for c in cmds)
    assert any("bootout user/501/com.mothership.daemon" in c for c in cmds)


def test_macos_query_unreachable_never_absent(tmp_path):
    """A running daemon must not render 'absent' just because launchctl can't
    be reached from this session."""
    sup, _ = _mac(tmp_path, {"print": OSError("launchctl gone")})
    assert sup.query().state == "unreachable"
    sup2, _ = _mac(tmp_path, {"print": _fail(stderr="Could not find service")})
    assert sup2.query().state == "absent"
    sup3, _ = _mac(tmp_path, {"print": _ok("state = running\npid = 4242\n")})
    assert sup3.query().state == "active"


def test_macos_linger_not_applicable(tmp_path):
    sup, _ = _mac(tmp_path)
    assert sup.linger_state() == "unknown"


def test_logs_tail_spans_rotated_siblings(tmp_path):
    log_dir = tmp_path / ".mothership" / "daemon" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "daemon.log.2").write_text("oldest-1\noldest-2\n")
    (log_dir / "daemon.log.1").write_text("middle-1\n")
    (log_dir / "daemon.log").write_text("newest-1\nnewest-2\n")
    sup, _ = _linux(tmp_path)
    assert sup.logs_tail(3) == ["middle-1", "newest-1", "newest-2"]
    assert sup.logs_tail(50) == ["oldest-1", "oldest-2", "middle-1", "newest-1", "newest-2"]


def test_pick_supervisor():
    assert isinstance(pick_supervisor(platform="linux"), SystemdUserSupervisor)
    assert isinstance(pick_supervisor(platform="darwin"), LaunchdSupervisor)


def test_macos_install_creates_log_dir(tmp_path):
    """launchd opens StandardOutPath itself before exec — a missing log dir
    means the RunAtLoad job silently never starts on a fresh account."""
    from mship.core.daemon.paths import daemon_log_dir

    sup, _ = _mac(tmp_path)
    sup.install(["/venv/bin/mshipd"])
    assert daemon_log_dir(tmp_path).is_dir()


def test_macos_start_rebootstraps_after_stop(tmp_path):
    """stop() boots the service out of the domain; start()/restart() must
    re-bootstrap an absent service before kickstarting it."""
    sup, rec = _mac(tmp_path, {"print": _fail(stderr="Could not find service")})
    sup.start()
    cmds = [" ".join(c) for c in rec.calls]
    assert any("bootstrap user/501" in c for c in cmds)
    assert any("kickstart user/501" in c for c in cmds)

    sup2, rec2 = _mac(tmp_path, {"print": _fail(stderr="Could not find service")})
    sup2.restart()
    cmds2 = [" ".join(c) for c in rec2.calls]
    assert any("bootstrap user/501" in c for c in cmds2)


def test_macos_start_skips_bootstrap_when_loaded(tmp_path):
    sup, rec = _mac(tmp_path, {"print": _ok("state = running\npid = 1\n")})
    sup.start()
    cmds = [" ".join(c) for c in rec.calls]
    assert not any("bootstrap" in c for c in cmds)


def test_linux_query_unit_not_found_is_absent(tmp_path):
    """A reachable manager answering 'not found' for an uninstalled unit is
    absent, not unreachable (the pre-install `daemon status` case)."""
    sup, _ = _linux(tmp_path, {"show mship-daemon": _fail(stderr="Unit mship-daemon.service could not be found.")})
    assert sup.query().state == "absent"


def test_macos_reinstall_unloads_first(tmp_path):
    """launchd rejects a duplicate bootstrap of a loaded label and the running
    job would keep the OLD plist — install boots the label out first
    (tolerated when not loaded), then bootstraps the fresh plist."""
    sup, rec = _mac(tmp_path)
    sup.install(["/venv/bin/mshipd"])
    cmds = [" ".join(c) for c in rec.calls]
    bootout_i = next(i for i, c in enumerate(cmds) if "bootout" in c)
    bootstrap_i = next(i for i, c in enumerate(cmds) if "bootstrap" in c)
    assert bootout_i < bootstrap_i

    # bootout failing (label not loaded — the FIRST install) must not block.
    sup2, rec2 = _mac(tmp_path, {"launchctl bootout": _fail(stderr="Boot-out failed: 3: No such process")})
    sup2.install(["/venv/bin/mshipd"])
    assert any("bootstrap" in " ".join(c) for c in rec2.calls)


def test_linux_user_defaults_to_uid_not_env(tmp_path, monkeypatch):
    """getpass.getuser() trusts LOGNAME/USER; a spoofed env must not make
    enable-linger target another account while systemctl targets this uid."""
    import os
    import pwd

    monkeypatch.setenv("LOGNAME", "someone-else")
    monkeypatch.setenv("USER", "someone-else")
    rec = Recorder({"show-user": _ok("Linger=yes\n")})
    sup = SystemdUserSupervisor(home=tmp_path, run_cmd=rec)
    assert sup._user == pwd.getpwuid(os.getuid()).pw_name
    assert sup._user != "someone-else"


def test_logs_tail_includes_launchd_pre_exec_captures(tmp_path):
    """A pre-exec failure (missing executable) only lands in launchd.err.log —
    `daemon logs` must surface it, not return nothing on a failed start."""
    log_dir = tmp_path / ".mothership" / "daemon" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "daemon.log").write_text("app-line\n")
    (log_dir / "launchd.err.log").write_text("posix_spawn: No such file or directory\n")
    sup, _ = _linux(tmp_path)
    lines = sup.logs_tail(10)
    assert "app-line" in lines
    assert "posix_spawn: No such file or directory" in lines

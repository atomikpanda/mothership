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


def test_logs_tail_includes_launchd_stderr_captures(tmp_path):
    """Early-exit stderr (process spawned, died before Python logging) lands
    only in launchd.err.log — `daemon logs` must surface it. (A posix_spawn
    failure produces no child and lands only in the unified log/journald —
    documented, not capturable here.)"""
    log_dir = tmp_path / ".mothership" / "daemon" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "daemon.log").write_text("app-line\n")
    (log_dir / "launchd.err.log").write_text("posix_spawn: No such file or directory\n")
    sup, _ = _linux(tmp_path)
    lines = sup.logs_tail(10)
    assert "app-line" in lines
    assert "posix_spawn: No such file or directory" in lines


def test_uid_without_passwd_entry_does_not_crash_commands(tmp_path, monkeypatch):
    """Arbitrary container UIDs have no NSS entry; the supervisor is built for
    EVERY daemon command, so status/logs must keep working."""
    import pwd

    monkeypatch.setattr(pwd, "getpwuid", lambda uid: (_ for _ in ()).throw(KeyError(uid)))
    rec = Recorder({"show mship-daemon": _ok("ActiveState=active\nSubState=running\n")})
    sup = SystemdUserSupervisor(home=tmp_path, run_cmd=rec)
    assert sup._user is None
    assert sup.query().state == "active"      # status still works
    assert sup.linger_state() == "unknown"    # nothing to query, no crash
    assert sup.logs_tail(5) == []


def test_install_fails_loudly_without_passwd_entry(tmp_path, monkeypatch):
    """Linger is mandatory, so an uninstallable environment must say so, not silently
    install a unit that dies on logout."""
    import pwd

    monkeypatch.setattr(pwd, "getpwuid", lambda uid: (_ for _ in ()).throw(KeyError(uid)))
    rec = Recorder()
    sup = SystemdUserSupervisor(home=tmp_path, run_cmd=rec)
    with pytest.raises(DaemonSupervisorError, match="passwd entry"):
        sup.install(["/venv/bin/mshipd"])
    # Nothing was mutated: no unit written, no daemon-reload, no enable — an
    # enabled unit without linger would start and die on every login.
    assert not (tmp_path / ".config" / "systemd" / "user" / "mship-daemon.service").exists()
    assert rec.calls == []


def test_stale_launchd_capture_does_not_hide_fresh_daemon_log(tmp_path):
    """A big stale launchd.err.log must not crowd the current daemon.log out of
    the -n tail (ordering is by mtime, not 'launchd always last')."""
    import os
    import time

    log_dir = tmp_path / ".mothership" / "daemon" / "logs"
    log_dir.mkdir(parents=True)
    stale = log_dir / "launchd.err.log"
    stale.write_text("\n".join(f"stale-{i}" for i in range(10)) + "\n")
    fresh = log_dir / "daemon.log"
    fresh.write_text("fresh-1\nfresh-2\n")
    old = time.time() - 3600
    os.utime(stale, (old, old))  # stale capture is genuinely older

    sup, _ = _linux(tmp_path)
    tail = sup.logs_tail(2)
    assert tail == ["fresh-1", "fresh-2"], tail


def test_uid_username_is_none_without_pwd_module(monkeypatch):
    """Windows has no `pwd`; mship.cli imports this module for EVERY command, so
    the import must be lazy and failure-tolerant."""
    import builtins

    import mship.core.daemon.supervisor as sup_mod

    real_import = builtins.__import__

    def no_pwd(name, *a, **kw):
        if name == "pwd":
            raise ModuleNotFoundError("No module named 'pwd'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_pwd)
    assert sup_mod._uid_username() is None


def test_supervisor_module_has_no_toplevel_pwd_import():
    """Static guard for the same class: a top-level `import pwd` would break
    the whole CLI on Windows at import time, before Typer dispatch."""
    import ast
    from pathlib import Path as _P

    src = _P(sup_module_path()).read_text()
    tree = ast.parse(src)
    toplevel = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in toplevel if isinstance(n, ast.Import) for a in n.names}
    assert "pwd" not in names


def sup_module_path() -> str:
    import mship.core.daemon.supervisor as sup_mod

    return sup_mod.__file__


def test_logs_tail_reads_only_the_tail_of_a_huge_file(tmp_path):
    """A crash-looping macOS daemon can leave a huge launchd capture; printing
    N lines must not pull the whole file into memory. Proven by content: the
    early megabytes are simply never returned, and the read is bounded by
    _TAIL_READ_BYTES (not monkeypatching builtins.open — pytest uses it too)."""
    import mship.core.daemon.supervisor as sup_mod

    log_dir = tmp_path / ".mothership" / "daemon" / "logs"
    log_dir.mkdir(parents=True)
    big = log_dir / "daemon.log"
    filler = "OLD-" + "x" * 96 + "\n"
    with open(big, "w") as fh:
        fh.write(filler * ((sup_mod._TAIL_READ_BYTES // len(filler)) + 500))
        fh.write("NEW-1\nNEW-2\nNEW-3\n")
    assert big.stat().st_size > sup_mod._TAIL_READ_BYTES

    sup, _ = _linux(tmp_path)
    assert sup.logs_tail(3) == ["NEW-1", "NEW-2", "NEW-3"]

    # the bound itself: a full-file read would yield far more than this
    lines = sup_mod._tail_lines(big, 10**9)
    assert len("\n".join(lines).encode()) <= sup_mod._TAIL_READ_BYTES
    # the partial first line is dropped: every returned line is a WHOLE line
    assert all(len(l) == len(filler) - 1 for l in lines if l.startswith("OLD-"))


def test_logs_tail_read_is_bounded_not_just_the_seek(tmp_path):
    """Concurrent appends between tell() and read() must not enlarge the read:
    the bound is on read(), not only on the seek offset."""
    import mship.core.daemon.supervisor as sup_mod

    log_dir = tmp_path / ".mothership" / "daemon" / "logs"
    log_dir.mkdir(parents=True)
    f = log_dir / "daemon.log"
    f.write_text("seed\n")

    real_open = open
    grown = {"done": False}

    class GrowingFile:
        """Appends a megabyte between tell() and read(), like a live writer."""

        def __init__(self, fh):
            self._fh = fh

        def seek(self, *a):
            return self._fh.seek(*a)

        def tell(self):
            return self._fh.tell()

        def read(self, *a):
            if not grown["done"]:
                grown["done"] = True
                with real_open(f, "ab") as w:
                    w.write(b"z" * (2 * sup_mod._TAIL_READ_BYTES))
            return self._fh.read(*a)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._fh.close()

    monkeyed = sup_mod.open if hasattr(sup_mod, "open") else None
    assert monkeyed is None  # uses builtins; wrap via the module namespace
    sup_mod.open = lambda p, mode="r", *a, **kw: GrowingFile(real_open(p, mode, *a, **kw))
    try:
        lines = sup_mod._tail_lines(f, 10)
    finally:
        del sup_mod.open
    assert len("\n".join(lines).encode()) <= sup_mod._TAIL_READ_BYTES


def test_logs_tail_trims_oversized_capture(tmp_path):
    """A daemon that never reaches main() can't trim its own capture, so the
    operator-facing path does it."""
    from mship.core.daemon.log_capture import LAUNCHD_CAPTURE_MAX_BYTES

    log_dir = tmp_path / ".mothership" / "daemon" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "daemon.log").write_text("app\n")
    huge = log_dir / "launchd.err.log"
    huge.write_bytes(b"y" * (LAUNCHD_CAPTURE_MAX_BYTES + 2048))
    sup, _ = _linux(tmp_path)
    sup.logs_tail(5)
    assert huge.stat().st_size == 0

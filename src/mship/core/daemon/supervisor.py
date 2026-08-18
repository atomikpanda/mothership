"""OS-supervisor adapters: the single injectable boundary for every
systemctl/launchctl/loginctl invocation in the daemon feature.

Linux availability probes the USER MANAGER, not the binary: many containers and
minimal hosts ship `systemctl` with no user manager running (`docker exec`,
no-pam_systemd SSH), where any `--user` call dies with a bus error. macOS uses
the `user/<uid>` launchd domain, not `gui/<uid>` — gui-domain operations fail
over SSH with no GUI session ("Bootstrap failed: 5: Input/output error"),
which is exactly the headless provisioning scenario #469/#470 describe.

Crash-loop DETECTION is `history.py`'s job (OS-agnostic); `query()` only maps
raw supervisor state.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from mship.core.daemon.paths import daemon_log_dir
from mship.core.daemon.log_capture import trim_launchd_captures
from mship.core.daemon.units import (
    LAUNCHD_LABEL,
    SYSTEMD_UNIT_NAME,
    launchd_plist_path,
    render_launchd_plist,
    render_systemd_unit,
    systemd_unit_path,
)

_RUN_FALLBACK = "no supervisor is reachable — use `mship daemon run` for a foreground daemon"


class DaemonSupervisorError(RuntimeError):
    pass


@dataclass(frozen=True)
class SupervisorState:
    state: Literal["active", "failed", "absent", "unreachable"]
    detail: str = ""


# Read at most this much from the END of each log file: `logs_tail` must not
# pull a multi-megabyte launchd capture into memory to print 100 lines.
_TAIL_READ_BYTES = 256 * 1024


def _tail_lines(path: Path, n: int) -> list[str]:
    """Last-ish `n` lines without reading the whole file."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - _TAIL_READ_BYTES))
        # Bound the READ too, not just the seek: a concurrent append between
        # tell() and read() would otherwise hand back the enlarged file.
        chunk = fh.read(_TAIL_READ_BYTES)
    text = chunk.decode("utf-8", errors="replace")
    if size > _TAIL_READ_BYTES:
        text = text.split("\n", 1)[-1]  # drop the partial first line
    return text.splitlines()[-n:]


def _uid_username() -> str | None:
    """This UID's passwd name, or None.

    pwd first, NOT getpass.getuser(): getuser trusts LOGNAME/USER, so a
    stale/spoofed env would enable-linger for ANOTHER account while
    `systemctl --user` still targets this uid — reported success, daemon still
    dies on logout. But an arbitrary UID with no NSS/passwd entry is normal in
    containers, and `SystemdUserSupervisor` is constructed for EVERY daemon
    command: returning None there keeps `status`/`logs` working (linger checks
    degrade to "unknown"/loud install failure) instead of crashing the CLI.
    """
    try:
        import pwd  # Unix-only: imported lazily so `mship <anything>` still runs on Windows
    except ModuleNotFoundError:
        return None
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, AttributeError):  # no passwd entry / no getuid on this platform
        return None


def _default_run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, **kw)


class _BaseSupervisor:
    def __init__(self, *, home: Path, run_cmd: Callable = _default_run) -> None:
        self._home = home
        self._run = run_cmd

    def logs_tail(self, n: int) -> list[str]:
        """Last N lines across daemon.log + rotated siblings — pure Python, no
        journalctl (the phone-only-client case needs OS-independent logs)."""
        log_dir = daemon_log_dir(self._home)
        # A daemon that dies before main() cannot trim its own capture, so the
        # operator-facing path trims too (#475 review).
        trim_launchd_captures(log_dir)
        files = sorted(
            log_dir.glob("daemon.log*"),
            key=lambda p: int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0,
            reverse=True,  # highest numeric suffix = oldest first
        )
        # Early-exit output that never reaches Python logging (interpreter
        # starts but dies before _configure_logging) goes to stderr, which the
        # plist wires into launchd.*.log — include those captures. NOTE: a true
        # pre-exec failure (missing executable → posix_spawn error) produces NO
        # child process and lands only in launchd's unified log / journald;
        # `launchctl print` and `journalctl --user -u mship-daemon` are the
        # diagnostics there (docs/daemon.md).
        # Ordered by MTIME against the daemon logs rather than appended last:
        # a stale launchd.err.log with >= n lines would otherwise crowd the
        # current daemon.log out of the `-n` tail.
        launchd = sorted(log_dir.glob("launchd.*.log"))
        if launchd:
            def _mtime(p: Path) -> float:
                try:
                    return p.stat().st_mtime
                except OSError:
                    return 0.0

            files = sorted(files + launchd, key=_mtime)
        lines: list[str] = []
        for f in files:
            try:
                lines.extend(_tail_lines(f, n))
            except OSError:
                continue
        return lines[-n:]


class SystemdUserSupervisor(_BaseSupervisor):
    def __init__(
        self, *, home: Path, user: str | None = None, run_cmd: Callable = _default_run
    ) -> None:
        super().__init__(home=home, run_cmd=run_cmd)
        self._user = user or _uid_username()

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess:
        return self._run(["systemctl", "--user", *args])

    def _checked(self, *args: str) -> subprocess.CompletedProcess:
        try:
            r = self._systemctl(*args)
        except OSError as e:
            raise DaemonSupervisorError(f"systemctl --user {' '.join(args)} failed: {e}; {_RUN_FALLBACK}") from e
        if r.returncode != 0:
            raise DaemonSupervisorError(
                f"systemctl --user {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}; {_RUN_FALLBACK}"
            )
        return r

    def available(self) -> bool:
        """Any manager reply — even `degraded` (nonzero) — proves reachability;
        a bus/connection error (or no systemctl at all) means unavailable."""
        try:
            r = self._systemctl("is-system-running")
        except (OSError, Exception):
            return False
        reply = (r.stdout or "").strip()
        if reply and "connect to bus" not in (r.stderr or ""):
            return True
        return False

    def install(self, argv: list[str]) -> None:
        # Validate linger BEFORE any unit mutation: it is mandatory, so a setup
        # failure must not leave an enabled unit that starts and dies on logout.
        if self._user is None:
            raise DaemonSupervisorError(
                f"uid {os.getuid()} has no passwd entry, so `loginctl enable-linger` has no "
                "user to target — linger is mandatory (without it the daemon dies when your "
                "last session ends). Run as a real user, or use `mship daemon run`."
            )
        try:
            r = self._run(["loginctl", "enable-linger", self._user])
        except OSError as e:
            raise DaemonSupervisorError(f"loginctl enable-linger failed: {e}") from e
        if r.returncode != 0:
            raise DaemonSupervisorError(f"loginctl enable-linger failed: {r.stderr.strip()}")
        if self.linger_state() != "yes":
            raise DaemonSupervisorError(
                "loginctl enable-linger did not stick (Linger!=yes) — without linger the "
                "daemon dies when your last SSH session ends. Check `loginctl show-user`."
            )

        unit_path = systemd_unit_path(self._home)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(render_systemd_unit(argv))
        self._checked("daemon-reload")
        self._checked("enable", SYSTEMD_UNIT_NAME.removesuffix(".service"))

    def start(self) -> None:
        self._checked("start", SYSTEMD_UNIT_NAME.removesuffix(".service"))

    def stop(self) -> None:
        self._checked("stop", SYSTEMD_UNIT_NAME.removesuffix(".service"))

    def restart(self) -> None:
        self._checked("restart", SYSTEMD_UNIT_NAME.removesuffix(".service"))

    def query(self) -> SupervisorState:
        try:
            r = self._systemctl(
                "show", SYSTEMD_UNIT_NAME.removesuffix(".service"), "--property=ActiveState,SubState"
            )
        except OSError as e:
            return SupervisorState("unreachable", str(e))
        if r.returncode != 0:
            # A reachable manager answers "not found" for an uninstalled unit —
            # that is absent, not unreachable (pre-install `daemon status`).
            error = (r.stderr or r.stdout or "").strip()
            if "not found" in error.lower() or "could not be found" in error.lower():
                return SupervisorState("absent", error)
            return SupervisorState("unreachable", error)
        props = dict(
            line.split("=", 1) for line in r.stdout.splitlines() if "=" in line
        )
        active = props.get("ActiveState")
        if active is None:
            return SupervisorState("absent", "unparseable systemctl show output")
        if active == "active":
            return SupervisorState("active", props.get("SubState", ""))
        if active == "failed":
            return SupervisorState("failed", props.get("SubState", ""))
        return SupervisorState("absent", f"{active}/{props.get('SubState', '')}")

    def linger_state(self) -> Literal["yes", "no", "unknown"]:
        if self._user is None:
            return "unknown"  # no passwd entry (container UID) — nothing to query
        try:
            r = self._run(["loginctl", "show-user", self._user, "--property=Linger"])
        except OSError:
            return "unknown"
        if r.returncode != 0:
            return "unknown"
        value = r.stdout.strip().removeprefix("Linger=")
        return value if value in ("yes", "no") else "unknown"


class LaunchdSupervisor(_BaseSupervisor):
    def __init__(self, *, home: Path, uid: int | None = None, run_cmd: Callable = _default_run) -> None:
        super().__init__(home=home, run_cmd=run_cmd)
        self._uid = uid if uid is not None else os.getuid()

    @property
    def _target(self) -> str:
        return f"user/{self._uid}/{LAUNCHD_LABEL}"

    def _launchctl(self, *args: str) -> subprocess.CompletedProcess:
        return self._run(["launchctl", *args])

    def _checked(self, *args: str) -> subprocess.CompletedProcess:
        try:
            r = self._launchctl(*args)
        except OSError as e:
            raise DaemonSupervisorError(f"launchctl {' '.join(args)} failed: {e}; {_RUN_FALLBACK}") from e
        if r.returncode != 0:
            raise DaemonSupervisorError(
                f"launchctl {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}; {_RUN_FALLBACK}"
            )
        return r

    def available(self) -> bool:
        try:
            r = self._launchctl("print", f"user/{self._uid}")
        except (OSError, Exception):
            return False
        return r.returncode == 0

    def install(self, argv: list[str]) -> None:
        plist_path = launchd_plist_path(self._home)
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        # launchd opens StandardOutPath/StandardErrorPath itself before exec —
        # on a fresh account the job silently never starts if this dir is missing.
        log_dir = daemon_log_dir(self._home)
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        plist_path.write_text(render_launchd_plist(argv, log_dir))
        # Re-install: launchd rejects a duplicate bootstrap of a loaded label,
        # and the running job would keep the OLD plist. Boot the label out
        # first (tolerated when not loaded), then bootstrap the new plist.
        try:
            self._launchctl("bootout", self._target)
        except OSError:
            pass
        # user/<uid>, never gui/<uid>: bootstrap must work over SSH with no GUI
        # session (the headless provisioning path).
        self._checked("bootstrap", f"user/{self._uid}", str(plist_path))

    def start(self) -> None:
        # stop() boots the service OUT of the domain (the only true stop under
        # KeepAlive — a signal-killed job would just be relaunched), so start
        # must re-bootstrap when the service is no longer loaded.
        if self.query().state == "absent":
            self._checked("bootstrap", f"user/{self._uid}", str(launchd_plist_path(self._home)))
        self._checked("kickstart", self._target)

    def stop(self) -> None:
        self._checked("bootout", self._target)

    def restart(self) -> None:
        if self.query().state == "absent":  # same unloaded-target class as start()
            self._checked("bootstrap", f"user/{self._uid}", str(launchd_plist_path(self._home)))
        self._checked("kickstart", "-k", self._target)

    def query(self) -> SupervisorState:
        try:
            r = self._launchctl("print", self._target)
        except OSError as e:
            return SupervisorState("unreachable", str(e))
        if r.returncode != 0:
            err = (r.stderr or "") + (r.stdout or "")
            if "Could not find service" in err:
                return SupervisorState("absent", err.strip())
            return SupervisorState("unreachable", err.strip())
        if "state = running" in r.stdout:
            return SupervisorState("active")
        return SupervisorState("absent", "loaded but not running")

    def linger_state(self) -> Literal["yes", "no", "unknown"]:
        return "unknown"  # not applicable on macOS

    def linger_supported(self) -> bool:
        return False


def pick_supervisor(*, home: Path | None = None, platform: str = sys.platform):
    home = home if home is not None else Path.home()
    if platform == "darwin":
        return LaunchdSupervisor(home=home)
    return SystemdUserSupervisor(home=home)

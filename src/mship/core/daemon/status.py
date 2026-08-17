"""Daemon status assembly — pure over injected inputs.

The lease file is READ-ONLY JSON diagnostics here (`read_lease_record`); this
path never takes the lease flock — a transient flock from status could race a
starting daemon into its loser path. Liveness is decided by the socket probe +
supervisor state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Mapping

from mship.core.daemon.control import probe_control_socket
from mship.core.daemon.history import HistoryEntry, is_crash_looping, unclean_start_count
from mship.core.daemon.lease import LeaseInfo, read_lease_record
from mship.core.daemon.paths import daemon_socket_path, lease_path
from mship.core.daemon.supervisor import SupervisorState


def restart_blockers() -> list[str]:
    """The #473 recovery handoff: reasons a daemon restart must be refused
    (e.g. active unattended workers whose recovery semantics can't yet prove
    no-duplicate-launch). Empty in v1 — #473 fills the function, not the
    control flow: `mship daemon restart` already consults this gate."""
    return []


def probe_daemon(*, home: Path, env: Mapping[str, str]) -> dict | None:
    """Probe /health at the LEASE-recorded socket path; only with no lease at
    all fall back to the computed path. The daemon (systemd-provided
    XDG_RUNTIME_DIR) and the invoking shell can compute different paths — a
    healthy daemon must not render 'unresponsive' from env divergence."""
    record = read_lease_record(lease_path(home))
    if record is not None and record.socket_path:
        return probe_control_socket(record.socket_path)
    return probe_control_socket(daemon_socket_path(env, home))


_LOOP_WINDOW_S = 600


@dataclass(frozen=True)
class DaemonStatus:
    running: bool
    supervised: bool
    compatible: bool
    pid: int | None
    daemon_version: str | None
    cli_version: str
    uptime_s: float | None
    socket: str | None
    supervisor: SupervisorState
    linger: Literal["yes", "no", "unknown"]
    unclean_starts: int
    lease_info: LeaseInfo | None
    lines: list[str] = field(default_factory=list)

    def render(self) -> str:
        return "\n".join(self.lines)


def build_status(
    *,
    supervisor_state: SupervisorState,
    linger: Literal["yes", "no", "unknown"],
    lease_info: LeaseInfo | None,
    health: dict | None,
    cli_version: str,
    history_entries: list[HistoryEntry],
    now: datetime,
) -> DaemonStatus:
    running = health is not None
    supervised = supervisor_state.state == "active"
    daemon_version = health.get("mship_version") if health else None
    compatible = daemon_version == cli_version if daemon_version else True
    unclean = unclean_start_count(history_entries, now, window_s=_LOOP_WINDOW_S)

    lines: list[str] = []
    if running:
        lines.append(f"daemon: running (pid {health['pid']}, v{daemon_version}, uptime {int(health.get('uptime_s', 0))}s)")
        lines.append(f"socket: {health.get('socket')}")
        if not supervised:
            lines.append(
                "warning: running outside the supervisor — will not survive logout/reboot; "
                "run `mship daemon install` / `mship daemon start`"
            )
    elif lease_info is not None:
        # A stale lease alone never reads as "already running" — but a record
        # with no answering socket is worth distinguishing from nothing at all.
        lines.append(f"daemon: unresponsive (lease pid {lease_info.pid}, socket not answering)")
    else:
        lines.append("daemon: not running")
    lines.append(f"supervisor: {supervisor_state.state}" + (f" ({supervisor_state.detail})" if supervisor_state.detail else ""))
    if not compatible:
        lines.append(f"restart required: daemon v{daemon_version}, CLI v{cli_version}")
    if is_crash_looping(history_entries, now, window_s=_LOOP_WINDOW_S):
        lines.append(f"crash loop: {unclean} unclean starts in last 10m")
    elif unclean:
        # Below the loop threshold: informational, not an alarm — one kill -9
        # or startup failure is not a crash loop.
        lines.append(f"unclean starts: {unclean} in last 10m")
    if linger == "no":
        lines.append("warning: linger is OFF — the daemon dies when your last SSH session ends; run `loginctl enable-linger`")
    elif linger == "unknown":
        lines.append("linger: unknown")
    lines.append("tunnel: not configured (#471)")
    lines.append("workspaces: registry pending (#472)")
    lines.append("runner: not configured (#473)")

    return DaemonStatus(
        running=running,
        supervised=supervised,
        compatible=compatible,
        pid=health.get("pid") if health else (lease_info.pid if lease_info else None),
        daemon_version=daemon_version,
        cli_version=cli_version,
        uptime_s=health.get("uptime_s") if health else None,
        socket=health.get("socket") if health else None,
        supervisor=supervisor_state,
        linger=linger,
        unclean_starts=unclean,
        lease_info=lease_info,
        lines=lines,
    )

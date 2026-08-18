"""`mship daemon` — lifecycle surface for the per-host Mothership daemon (#470).

Thin over the injectable supervisor seam; core raises typed exceptions, only
this layer raises `typer.Exit`. `get_container` is accepted for the house
`register(app, get_container)` shape but NEVER resolved into a workspace — the
daemon is workspace-agnostic until #472.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import typer

import mship
from mship.core.daemon.history import read_history
from mship.core.daemon.log_capture import rotate_launchd_captures
from mship.core.daemon.lease import read_lease_record
from mship.core.daemon.paths import daemon_log_dir, lease_path, start_history_path
from mship.core.daemon.registry import DaemonConfig
from mship.core.daemon.status import build_status, probe_daemon, restart_blockers
from mship.core.daemon.supervisor import DaemonSupervisorError, pick_supervisor
from mship.core.daemon.units import DaemonExecResolutionError, resolve_mshipd_argv
from mship.cli.output import Output


def _daemon_main() -> int:
    from mship.core.daemon.run import main

    return main()


def _snapshot_file(path: Path) -> tuple[Path, bytes | None]:
    try:
        previous = path.read_bytes()
    except FileNotFoundError:
        previous = None
    return path, previous


def _restore_daemon_credentials(
    snapshots: list[tuple[Path, bytes | None]] | None,
) -> None:
    if snapshots is None:
        return
    from mship.core.daemon.host_app import _atomic_write_owner_file

    for path, previous in reversed(snapshots):
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write_owner_file(path, previous)


def _resolve_daemon_credential_overrides(
    output: Output,
) -> tuple[str | None, str | None, str | None]:
    from mship.core.daemon.host_app import load_host_token_override

    try:
        token = load_host_token_override(os.environ)
    except ValueError as exc:
        output.error(str(exc))
        raise typer.Exit(1)
    app_id = private_key = None
    if os.environ.get("MSHIP_GH_APP_ID") or os.environ.get("MSHIP_GH_APP_KEY"):
        from mship.cli.serve import _read_gh_app_creds

        app_id, private_key = _read_gh_app_creds(output)
    return token, app_id, private_key


def _persist_daemon_credential_overrides(
    home: Path,
    output: Output,
    resolved: tuple[str | None, str | None, str | None] | None = None,
) -> list[tuple[Path, bytes | None]]:
    from mship.core.daemon.host_app import (
        _credential_paths,
        persist_gh_app_credentials,
        persist_host_token,
    )

    token_path, app_id_path, app_key_path = _credential_paths(home)
    snapshots: list[tuple[Path, bytes | None]] = []
    token, app_id, private_key = (
        resolved
        if resolved is not None
        else _resolve_daemon_credential_overrides(output)
    )
    try:
        if token:
            snapshots.append(_snapshot_file(token_path))
            persist_host_token(home, token)
        if app_id and private_key:
            snapshots.extend([
                _snapshot_file(app_id_path),
                _snapshot_file(app_key_path),
            ])
            persist_gh_app_credentials(home, app_id, private_key)
    except BaseException:
        _restore_daemon_credentials(snapshots)
        raise
    return snapshots


def _validate_scan_roots(cfg, output: Output) -> None:
    from mship.core.daemon.discovery import ScanRootError, scan_roots

    try:
        scan_roots(cfg)
    except ScanRootError as exc:
        output.error(str(exc))
        raise typer.Exit(1)


def _validate_daemon_config(home: Path, output: Output):
    from mship.core.daemon.registry import load_daemon_config

    try:
        cfg = load_daemon_config(home)
    except ValueError as exc:
        output.error(str(exc))
        raise typer.Exit(1)
    _validate_scan_roots(cfg, output)
    return cfg


def _resolve_effective_daemon_credentials(
    home: Path, output: Output
) -> tuple[str | None, str | None, str | None]:
    from mship.core.daemon.host_app import load_gh_app_credentials

    resolved = _resolve_daemon_credential_overrides(output)
    if resolved[1] is None:
        try:
            load_gh_app_credentials(home, env={})
        except ValueError as exc:
            output.error(str(exc))
            raise typer.Exit(1)
    return resolved


def _preflight_daemon_start(
    home: Path, output: Output
) -> tuple[
    DaemonConfig,
    tuple[str | None, str | None, str | None],
]:
    cfg = _validate_daemon_config(home, output)
    return cfg, _resolve_effective_daemon_credentials(home, output)


def register(parent: typer.Typer, get_container):
    daemon_app = typer.Typer(
        name="daemon",
        help="Manage the per-host Mothership daemon (one supervised mshipd per OS user).",
        no_args_is_help=True,
    )

    def _supervisor():
        return pick_supervisor()

    @daemon_app.command("install")
    def install(
        scan_root: list[str] = typer.Option(
            None, "--scan-root",
            help="Absolute directory to scan for mothership.yaml workspaces (repeatable). Seeds the daemon config.",
        ),
        serve: str = typer.Option(
            None, "--serve", metavar="HOST:PORT",
            help="Also bind the workspace-addressed HTTP API on HOST:PORT (pre-#471 phone reachability). Without it the daemon is control-socket only.",
        ),
        relay: str = typer.Option(
            None, "--relay", metavar="HOST",
            help="Register this host with the relay at HOST and keep an ssh -R tunnel to it up. Needs a --serve bind (here or from an earlier install). Like --serve, a changed relay takes effect on `mship daemon restart`.",
        ),
    ):
        """Install + enable the OS-user supervisor unit (systemd --user / launchd)."""
        out = Output()
        serve_cfg = None
        if serve is not None:
            host, sep, port_s = serve.rpartition(":")
            if not sep or not host or not port_s.isdigit():
                out.error(f"--serve expects HOST:PORT, got {serve!r}")
                raise typer.Exit(1)
            port = int(port_s)
            if not 1 <= port <= 65535:
                out.error(f"--serve expects HOST:PORT, got {serve!r}")
                raise typer.Exit(1)
            serve_cfg = {"host": host, "port": port}
        roots = list(scan_root or [])
        bad = [r for r in roots if not Path(r).is_absolute()]
        if bad:
            out.error(f"--scan-root must be absolute: {bad}")
            raise typer.Exit(1)
        sup = _supervisor()
        if not sup.available():
            out.error(
                "no reachable OS supervisor (user manager) on this host — persistence is "
                "not possible here; use `mship daemon run` for a foreground daemon."
            )
            raise typer.Exit(1)
        try:
            argv = resolve_mshipd_argv()
        except DaemonExecResolutionError as e:
            out.error(str(e))
            raise typer.Exit(1)
        home = Path.home()
        previous_cfg, resolved_credentials = _preflight_daemon_start(
            home, out
        )
        if any(resolved_credentials) and sup.query().state == "active":
            out.error(
                "daemon is already active; credential overrides were not "
                "persisted — use `mship daemon restart` to apply them"
            )
            raise typer.Exit(1)
        relay_host = relay.strip() if relay is not None else None
        if relay is not None and not relay_host:
            out.error(f"--relay expects HOST, got {relay!r}")
            raise typer.Exit(1)
        relay_cfg = {"host": relay_host} if relay_host else None
        merged = None
        if roots or serve_cfg is not None or relay_cfg is not None:
            from mship.core.daemon.registry import save_daemon_config

            merged = DaemonConfig(
                scan_roots=sorted(set(previous_cfg.scan_roots) | set(roots)),
                ignore_globs=previous_cfg.ignore_globs,
                max_depth=previous_cfg.max_depth,
                serve=serve_cfg if serve_cfg is not None else previous_cfg.serve,
                relay=relay_cfg if relay_cfg is not None else previous_cfg.relay,
            )
            # The MERGED config, not the flag: `--relay` on a host whose
            # `serve:` an earlier install already set is the normal
            # incremental-provisioning path, and validating the flag alone
            # would reject it.
            if merged.relay is not None and merged.serve is None:
                out.error(
                    "--relay needs a local bind to forward: pass --serve HOST:PORT "
                    "(here or in an earlier install)"
                )
                raise typer.Exit(1)
            _validate_scan_roots(merged, out)

            # Launchd's bootstrap starts a RunAtLoad job immediately. Persist
            # validated config first so that first process sees the requested
            # roots/bind rather than the old snapshot.
            save_daemon_config(home, merged)
        credential_snapshots = None
        try:
            credential_snapshots = _persist_daemon_credential_overrides(
                Path.home(), out, resolved_credentials
            )
            sup.install(argv)
        except (DaemonSupervisorError, OSError) as e:
            if merged is not None:
                save_daemon_config(home, previous_cfg)
            _restore_daemon_credentials(credential_snapshots)
            out.error(str(e))
            raise typer.Exit(1)
        if merged is not None:
            out.print(
                f"daemon config seeded: {len(merged.scan_roots)} scan root(s)"
                + (", serve bind set" if merged.serve else "")
                + (f", relay {merged.relay['host']}" if merged.relay else "")
            )
        out.print("daemon installed and enabled — start it with `mship daemon start`")

    @daemon_app.command("start")
    def start():
        out = Output()
        _, resolved_credentials = _preflight_daemon_start(Path.home(), out)
        sup = _supervisor()
        if any(resolved_credentials) and sup.query().state == "active":
            out.error(
                "daemon is already active; credential overrides were not "
                "persisted — use `mship daemon restart` to apply them"
            )
            raise typer.Exit(1)
        credential_snapshots = None
        try:
            credential_snapshots = _persist_daemon_credential_overrides(
                Path.home(), out, resolved_credentials
            )
            sup.start()
        except (DaemonSupervisorError, OSError) as e:
            _restore_daemon_credentials(credential_snapshots)
            out.error(str(e))
            raise typer.Exit(1)
        out.print("daemon started")

    @daemon_app.command("stop")
    def stop():
        out = Output()
        try:
            _supervisor().stop()
        except DaemonSupervisorError as e:
            out.error(str(e))
            raise typer.Exit(1)
        out.print("daemon stopped")

    @daemon_app.command("restart")
    def restart():
        out = Output()
        blockers = restart_blockers()
        if blockers:
            out.error("restart refused:\n" + "\n".join(f"  - {b}" for b in blockers))
            raise typer.Exit(1)
        _, resolved_credentials = _preflight_daemon_start(Path.home(), out)
        credential_snapshots = None
        try:
            credential_snapshots = _persist_daemon_credential_overrides(
                Path.home(), out, resolved_credentials
            )
            _supervisor().restart()
        except (DaemonSupervisorError, OSError) as e:
            _restore_daemon_credentials(credential_snapshots)
            out.error(str(e))
            raise typer.Exit(1)
        out.print("daemon restarted")

    @daemon_app.command("status")
    def status():
        out = Output()
        home = Path.home()
        rotate_launchd_captures(daemon_log_dir(home))
        sup = _supervisor()
        from mship.core.daemon.paths import registry_path
        from mship.core.daemon.registry import RegistryReadError, RegistryStore

        try:
            entries = [
                e
                for e in RegistryStore(registry_path(home)).load().entries
                if not e.ignored
            ]
        except RegistryReadError:
            workspaces = None
        else:
            workspaces = (
                len(entries),
                len([e for e in entries if e.state == "degraded"]),
            )
        st = build_status(
            workspaces=workspaces,
            supervisor_state=sup.query(),
            linger=sup.linger_state(),
            lease_info=read_lease_record(lease_path(home)),
            health=probe_daemon(home=home, env=os.environ),
            cli_version=mship.__version__,
            history_entries=read_history(start_history_path(home)),
            now=datetime.now(timezone.utc),
        )
        if out.json_mode:
            out.json(
                {
                    "running": st.running,
                    "supervised": st.supervised,
                    "compatible": st.compatible,
                    "pid": st.pid,
                    "daemon_version": st.daemon_version,
                    "cli_version": st.cli_version,
                    "uptime_s": st.uptime_s,
                    "socket": st.socket,
                    "supervisor": {"state": st.supervisor.state, "detail": st.supervisor.detail},
                    "linger": st.linger,
                    "unclean_starts": st.unclean_starts,
                    "lines": st.lines,
                }
            )
        else:
            out.print(st.render())

    @daemon_app.command("logs")
    def logs(n: int = typer.Option(100, "-n", "--lines", help="Lines to show.")):
        out = Output()
        for line in _supervisor().logs_tail(n):
            out.print(line)

    @daemon_app.command("run")
    def run():
        """Run the daemon in the foreground (debug / no-supervisor hosts)."""
        raise typer.Exit(_daemon_main())

    parent.add_typer(daemon_app, rich_help_panel="Runtime")

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
from mship.core.daemon.lease import read_lease_record
from mship.core.daemon.paths import lease_path, start_history_path
from mship.core.daemon.status import build_status, probe_daemon, restart_blockers
from mship.core.daemon.supervisor import DaemonSupervisorError, pick_supervisor
from mship.core.daemon.units import DaemonExecResolutionError, resolve_mshipd_argv
from mship.cli.output import Output


def _daemon_main() -> int:
    from mship.core.daemon.run import main

    return main()


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
    ):
        """Install + enable the OS-user supervisor unit (systemd --user / launchd)."""
        out = Output()
        serve_cfg = None
        if serve is not None:
            host, sep, port_s = serve.rpartition(":")
            if not sep or not host or not port_s.isdigit():
                out.error(f"--serve expects HOST:PORT, got {serve!r}")
                raise typer.Exit(1)
            serve_cfg = {"host": host, "port": int(port_s)}
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
            sup.install(argv)
        except (DaemonExecResolutionError, DaemonSupervisorError) as e:
            out.error(str(e))
            raise typer.Exit(1)
        if roots or serve_cfg is not None:
            from mship.core.daemon.registry import DaemonConfig, load_daemon_config, save_daemon_config

            home = Path.home()
            cfg = load_daemon_config(home)
            merged = DaemonConfig(
                scan_roots=sorted(set(cfg.scan_roots) | set(roots)),
                ignore_globs=cfg.ignore_globs,
                max_depth=cfg.max_depth,
                serve=serve_cfg if serve_cfg is not None else cfg.serve,
            )
            save_daemon_config(home, merged)
            out.print(f"daemon config seeded: {len(merged.scan_roots)} scan root(s)" + (", serve bind set" if merged.serve else ""))
        out.print("daemon installed and enabled — start it with `mship daemon start`")

    @daemon_app.command("start")
    def start():
        out = Output()
        try:
            _supervisor().start()
        except DaemonSupervisorError as e:
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
        try:
            _supervisor().restart()
        except DaemonSupervisorError as e:
            out.error(str(e))
            raise typer.Exit(1)
        out.print("daemon restarted")

    @daemon_app.command("status")
    def status():
        out = Output()
        home = Path.home()
        sup = _supervisor()
        from mship.core.daemon.paths import registry_path
        from mship.core.daemon.registry import RegistryStore

        entries = [e for e in RegistryStore(registry_path(home)).load().entries if not e.ignored]
        st = build_status(
            workspaces=(len(entries), len([e for e in entries if e.state != "healthy"])),
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

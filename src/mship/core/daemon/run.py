"""`mshipd` — the daemon process entrypoint (same package as the CLI).

Import-minimal at module top: stdlib + paths/lease/history only. The
FastAPI/uvicorn/control imports are deferred into `main()` AFTER the history
append so a broken-upgrade ImportError still lands in history and the rotating
log (`test_broken_import_still_appends_history`).

Sequence: rotating logs → lease → loser path (probe holder; live → exit 0,
dead → exit 1) → history start → unlink stale socket → uvicorn over the unix
socket → clean-stop entry on normal return. SIGTERM relies on uvicorn's default
graceful shutdown — no bespoke signal code. The OS supervisor owns
restart/backoff; there is deliberately no retry loop here.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Mapping

from mship.core.daemon import history, lease as lease_mod, paths

log = logging.getLogger(__name__)

_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 3

# Loser-with-dead-holder → nonzero so the supervisor retries; a wedged
# non-serving holder must never park the unit "inactive-success" with zero
# daemons. Confirmed-live holder → 0 (the only supervisor-safe loser status:
# launchd KeepAlive.SuccessfulExit=false relaunches on ANY nonzero exit).
_EXIT_CONTENDED_DEAD = 1


def _probe(socket_path) -> dict | None:
    """Seam for tests; deferred import keeps module top stdlib-only."""
    from mship.core.daemon.control import probe_control_socket

    return probe_control_socket(socket_path)


def _import_server_stack():
    """Deferred FastAPI/uvicorn import — the seam the broken-upgrade test patches."""
    import uvicorn

    from mship.core.daemon.control import create_control_app

    return uvicorn, create_control_app


def _build_registry(home: Path):
    """Startup discovery (#472): load daemon config, scan, reconcile. Returns
    (store, rescan, serve_cfg). Never raises — a scan failure leaves an empty
    registry and gets logged (a bad candidate must not kill the daemon)."""
    from mship.core.daemon.discovery import scan_roots
    from mship.core.daemon.paths import registry_path
    from mship.core.daemon.registry import RegistryStore, load_daemon_config, reconcile

    store = RegistryStore(registry_path(home))
    try:
        cfg = load_daemon_config(home)
    except ValueError as e:
        log.error("daemon config invalid: %s — serving empty registry", e)
        return store, lambda: None, None

    def rescan():
        # Re-READ the config every time: `mship workspace refresh` exists
        # precisely so an edited config.yaml takes effect without a restart, so
        # closing over the startup snapshot would scan the old roots forever.
        # (A changed `serve:` bind still needs a restart — that's a process
        # boundary, and status reports the running bind.)
        try:
            current = load_daemon_config(home)
        except ValueError as e:
            log.error("daemon config invalid on refresh: %s — keeping previous roots", e)
            current = cfg
        reconcile(store, scan_roots(current), datetime.now(timezone.utc))

    try:
        rescan()
    except Exception:
        log.exception("workspace scan failed — serving current registry state")
    return store, rescan, cfg.serve


def _serve_forever(control_app, socket_path, host_app, serve_cfg) -> None:
    """Control app on the unix socket; when a TCP bind is configured, the
    workspace-addressed host app runs beside it under one asyncio loop."""
    import uvicorn

    if host_app is None or serve_cfg is None:
        uvicorn.run(control_app, uds=str(socket_path), log_config=None)
        return

    import asyncio

    async def _both():
        control = uvicorn.Server(uvicorn.Config(control_app, uds=str(socket_path), log_config=None))
        host = uvicorn.Server(uvicorn.Config(
            host_app, host=serve_cfg["host"], port=int(serve_cfg["port"]), log_config=None,
        ))
        control_app.state.set_serve_bound(False)
        control_task = asyncio.create_task(control.serve())
        host_task = asyncio.create_task(host.serve())
        try:
            while not host.started:
                if host_task.done():
                    control.should_exit = True
                    await asyncio.gather(control_task, return_exceptions=True)
                    failure = host_task.exception()
                    if failure is not None:
                        raise RuntimeError("TCP server failed to bind") from failure
                    raise RuntimeError("TCP server failed to bind")
                if control_task.done():
                    host.should_exit = True
                    await asyncio.gather(host_task, return_exceptions=True)
                    failure = control_task.exception()
                    if failure is not None:
                        raise RuntimeError("control server stopped before TCP bind") from failure
                    raise RuntimeError("control server stopped before TCP bind")
                await asyncio.sleep(0)

            control_app.state.set_serve_bound(True)
            done, _pending = await asyncio.wait(
                {control_task, host_task}, return_when=asyncio.FIRST_COMPLETED
            )
            unexpected = (
                control_task in done and not control.should_exit
            ) or (
                host_task in done and not host.should_exit
            )
            control.should_exit = True
            host.should_exit = True
            results = await asyncio.gather(
                control_task, host_task, return_exceptions=True
            )
            failure = next(
                (result for result in results if isinstance(result, BaseException)),
                None,
            )
            if failure is not None:
                raise RuntimeError("daemon server failed") from failure
            if unexpected:
                raise RuntimeError("daemon server stopped unexpectedly")
        finally:
            control_app.state.set_serve_bound(False)

    asyncio.run(_both())


def _configure_logging(home: Path) -> None:
    log_dir = paths.daemon_log_dir(home)
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(
        log_dir / "daemon.log", maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # Route uvicorn's own loggers into the same rotating file (uvicorn otherwise
    # installs stderr handlers — journald-only on Linux, discarded on macOS).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)


def main(home: Path | None = None, env: Mapping[str, str] | None = None) -> int:
    home = home if home is not None else Path.home()
    env = env if env is not None else os.environ
    _configure_logging(home)
    try:
        return _run(home, env)
    except Exception:
        # The one artifact needed to diagnose a crash loop: without this a
        # crashing daemon leaves an empty rotated log.
        log.exception("mshipd crashed")
        return 1


def _run(home: Path, env: Mapping[str, str]) -> int:
    import mship

    version = mship.__version__  # captured once at process start (the version boundary)
    socket_path = paths.daemon_socket_path(env, home, create=True)

    daemon_lease = lease_mod.DaemonLease(paths.lease_path(home))
    holder = daemon_lease.try_acquire(version=version, socket_path=str(socket_path))
    if holder is not None:
        probe_target = holder.socket_path or str(socket_path)
        if _probe(probe_target) is not None:
            log.info("mshipd already running (pid %s) — standing down", holder.pid)
            return 0
        log.error(
            "lease is held (pid %s) but its daemon never answered %s — contended-but-dead; exiting for supervisor retry",
            holder.pid,
            probe_target,
        )
        return _EXIT_CONTENDED_DEAD

    started_at = datetime.now(timezone.utc)
    history.append_start(paths.start_history_path(home), started_at)
    try:
        uvicorn, create_control_app = _import_server_stack()
    except BaseException:
        log.exception("mshipd failed to import its server stack (broken upgrade?)")
        return 1

    # Safe to unlink: winning the lease proves no live daemon owns the socket.
    try:
        socket_path.unlink()
        log.info("removed stale control socket %s", socket_path)
    except FileNotFoundError:
        pass

    store, rescan, serve_cfg = _build_registry(home)
    entries = store.load().entries
    log.info(
        "mshipd %s starting on %s (pid %s) — %d workspace(s) discovered",
        version, socket_path, os.getpid(), len(entries),
    )
    host_app = None
    if serve_cfg is not None:
        from mship.core.daemon.host_app import create_host_app, ensure_host_token

        host_app = create_host_app(store, auth_token=ensure_host_token(home), rescan=rescan)
    app = create_control_app(
        started_at=started_at, version=version, socket_path=str(socket_path),
        store=store, rescan=rescan, serve_bound=host_app is not None,
        after_rescan=(
            host_app.state.drop_stale_subapps if host_app is not None else None
        ),
    )
    _serve_forever(app, socket_path, host_app, serve_cfg)
    history.append_clean_stop(paths.start_history_path(home), datetime.now(timezone.utc))
    log.info("mshipd stopped cleanly")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    sys.exit(main())

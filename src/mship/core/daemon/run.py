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
from mship.core.daemon.log_capture import LAUNCHD_CAPTURE_MAX_BYTES, trim_launchd_captures

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
    # FIRST, before any heavier import or setup can fail: the crash loop this
    # guards against is frequently a broken import, and every relaunch appends
    # another traceback to the launchd capture. Logged once logging exists, so
    # the truncation is never silent.
    trimmed = trim_launchd_captures(paths.daemon_log_dir(home))
    _configure_logging(home)
    for name in trimmed:
        log.warning("truncated oversized launchd capture %s (>%d bytes)", name, LAUNCHD_CAPTURE_MAX_BYTES)
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

    log.info("mshipd %s starting on %s (pid %s)", version, socket_path, os.getpid())
    app = create_control_app(started_at=started_at, version=version, socket_path=str(socket_path))
    uvicorn.run(app, uds=str(socket_path), log_config=None)
    history.append_clean_stop(paths.start_history_path(home), datetime.now(timezone.utc))
    log.info("mshipd stopped cleanly")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    sys.exit(main())

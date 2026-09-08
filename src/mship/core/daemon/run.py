"""`mshipd` — the daemon process entrypoint (same package as the CLI).

Import-minimal at module top: stdlib + paths/lease/history only. The
FastAPI/uvicorn/control imports are deferred into `main()` AFTER the history
append so a broken-upgrade ImportError still lands in history and the rotating
log (`test_broken_import_still_appends_history`).

Sequence: rotating logs → lease → loser path (probe holder; live → exit 0,
dead → exit 1) → history start → unlink stale socket → servers (and, when a
relay is configured, the tunnel) over one asyncio loop → clean-stop entry on
normal return. The OS supervisor owns restart/backoff; there is deliberately no
retry loop here.

SIGTERM is no longer uvicorn's business alone (#471): its handlers only set
`should_exit` on the server that installed them, so a tunnel loop beside them
would never learn a stop was requested, the daemon would outlive
`TimeoutStopSec`, be SIGKILLed, and leave its `start_new_session=True` ssh child
orphaned on the subdomain — where it blocks the next start from claiming it. One
shared `anyio.Event` is the stop condition for everything.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from mship.core.daemon import history, lease as lease_mod, paths
from mship.core.daemon.log_capture import (
    LAUNCHD_CAPTURE_MAX_BYTES,
    rotate_launchd_captures,
)

if TYPE_CHECKING:
    import anyio

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
    """Startup discovery (#472): load daemon config, scan, reconcile.

    Invalid config clears the registry. An unavailable configured root raises
    without mutation so startup fails closed and the supervisor can retry.
    Per-candidate failures degrade entries rather than aborting the scan.
    """
    from mship.core.daemon.discovery import ScanRootError, scan_roots
    from mship.core.daemon.paths import registry_path
    from mship.core.daemon.registry import (
        DaemonConfigReadError,
        RegistryStore,
        load_daemon_config,
        reconcile,
    )

    store = RegistryStore(registry_path(home))

    def clear_registry() -> None:
        store.mutate(lambda state: state.entries.clear())

    try:
        cfg = load_daemon_config(home)
    except DaemonConfigReadError:
        log.exception("daemon config unreadable — registry unchanged")
        raise
    except ValueError as e:
        log.error("daemon config invalid: %s — serving empty registry", e)
        clear_registry()
        cfg = None

    def rescan():
        # Re-READ the config every time: `mship workspace refresh` exists
        # precisely so an edited config.yaml takes effect without a restart, so
        # closing over the startup snapshot would scan the old roots forever.
        # (A changed `serve:` bind still needs a restart — that's a process
        # boundary, and status reports the running bind.)
        try:
            current = load_daemon_config(home)
        except DaemonConfigReadError:
            log.exception("daemon config unreadable on refresh — registry unchanged")
            raise
        except ValueError:
            log.exception("daemon config invalid on refresh — registry unchanged")
            raise
        reconcile(store, scan_roots(current), datetime.now(timezone.utc))

    if cfg is None:
        # Keep the real callback: fixing config.yaml + control refresh must
        # recover discovery without requiring a process restart.
        return store, rescan, None

    try:
        rescan()
    except ScanRootError:
        log.exception(
            "configured scan root unavailable — registry unchanged; "
            "refusing daemon startup"
        )
        raise
    except Exception:
        log.exception("workspace scan failed — serving current registry state")
    return store, rescan, cfg.serve


def _relay_config(home: Path):
    """This host's `relay:` block as a `RelayConfig`, or None for no relay.

    Invalid configuration raises instead of impersonating an absent `relay:`
    block. A configured-but-broken tunnel must be visible as an error or stop
    startup, never be reported as deliberately disabled.
    """
    from mship.core.daemon.registry import load_daemon_config
    from mship.core.relay.config import RelayConfig

    return RelayConfig.from_mapping(load_daemon_config(home).relay)


def _build_tunnel(home: Path, relay_cfg, serve_cfg):
    """The relay tunnel + registration link (#471), or None without a relay.

    A configured relay without a local bind is invalid: there is nowhere for
    the reverse tunnel to forward. Construction failures propagate to `_run`,
    which keeps local service alive while publishing the tunnel error.
    """
    if relay_cfg is None:
        return None
    if serve_cfg is None:
        raise ValueError("relay needs a local serve bind")
    from mship.core.daemon.host_tunnel import HostTunnel
    from mship.core.daemon.relay_link import RelayLink
    from mship.core.relay import keys
    from mship.core.relay.tunnel import TunnelSupervisor, build_tunnel_argv

    link = RelayLink(home, relay_cfg)
    supervisor = TunnelSupervisor(
        # A callable, not a frozen list: an auto-reidentify moves the link
        # onto a NEW subdomain mid-run (AC4), and argv frozen at startup
        # would keep re-dialing the one this host no longer owns.
        argv=lambda: build_tunnel_argv(
            relay_cfg,
            subdomain=link.subdomain,
            local_host=serve_cfg["host"],
            local_port=int(serve_cfg["port"]),
            key_path=keys.relay_key_path(home),
        ),
        log_path=paths.tunnel_log_path(home),
    )
    return HostTunnel(link, supervisor)


def _tunnel_join_timeout() -> float:
    """Retained shutdown budget for the tunnel loop's bounded in-flight tick.

    The task group joins the non-abandoned worker before stopping its supervisor;
    this budget documents the underlying operation bounds, not permission to
    abandon a still-running tick. That worst case is the three
    relay calls a single tick can make (challenge, register, enroll), each with
    a whole-call deadline including owned resolver startup/resolution and its
    response body (resolver cancellation kills and reaps the child), plus one signing
    subprocess, and one key-generation subprocess on automatic clone recovery.
    Recovery does not revoke the incumbent's shared key, so it adds no relay
    calls. The tick can also make three fixed, bounded
    process-table snapshots (discovery, pre-TERM, pre-KILL) and the shared
    TERM/KILL exit waits. Still bounded,
    because a daemon that never returns is
    SIGKILLed by systemd — which is itself how an ssh child orphans onto the
    subdomain (#471 AC7).
    """
    from mship.core.daemon.host_tunnel import (
        MAX_PROCESS_LIST_CALLS_PER_REAP,
        ORPHAN_EXIT_TIMEOUT_S,
        PROCESS_LIST_TIMEOUT_S,
    )
    from mship.core.daemon.relay_link import HTTP_TIMEOUT_S
    from mship.core.relay.keys import KEYGEN_TIMEOUT_S
    from mship.core.relay.ssh_sig import SSH_KEYGEN_TIMEOUT_S

    return (
        3 * HTTP_TIMEOUT_S
        + MAX_PROCESS_LIST_CALLS_PER_REAP * PROCESS_LIST_TIMEOUT_S
        + 2 * ORPHAN_EXIT_TIMEOUT_S
        + SSH_KEYGEN_TIMEOUT_S
        + KEYGEN_TIMEOUT_S
    )


def _serve_forever(control_app, socket_path, host_app, serve_cfg, tunnel=None) -> None:
    """Run every long-lived part of the daemon on ONE asyncio loop.

    Always asyncio, even control-only: `uvicorn.run` owns the loop and returns
    only once the server is done, which leaves no seam to run a tunnel beside —
    and one startup/shutdown shape for all three configurations is one shutdown
    path to keep correct rather than three.
    """
    import anyio

    anyio.run(
        _serve,
        control_app,
        socket_path,
        host_app,
        serve_cfg,
        tunnel,
        backend="asyncio",
    )


@dataclass(frozen=True)
class _ServerOutcome:
    name: str
    started: bool
    clean: bool
    error: BaseException | None = None


class _ServerStoppedBeforeReady(Exception):
    def __init__(self, outcome: _ServerOutcome) -> None:
        super().__init__(outcome.name)
        self.outcome = outcome


async def _run_server_child(
    name,
    server,
    outcomes,
    terminal_outcomes,
    *,
    task_status: anyio.abc.TaskStatus[None],
) -> None:
    """Own one uvicorn server and expose its readiness to the root group."""
    import anyio

    completed = anyio.Event()
    outcome = None

    async def run_server() -> None:
        nonlocal outcome
        returned = False
        try:
            await server.serve()
            returned = True
        except anyio.get_cancelled_exc_class():
            raise
        except BaseException as error:
            outcome = _ServerOutcome(
                name=name,
                started=server.started,
                clean=server.should_exit,
                error=error,
            )
        else:
            outcome = _ServerOutcome(
                name=name,
                started=server.started,
                clean=server.should_exit,
            )
        finally:
            try:
                if not returned:
                    # Uvicorn calls shutdown only on a normal serve() return.
                    # Its ASGI lifespan and connections are asyncio-owned, so
                    # cancelling our child must still join that cleanup.
                    with anyio.CancelScope(shield=True):
                        if server.started:
                            await server.shutdown()
                        elif (lifespan := getattr(server, "lifespan", None)) is not None:
                            # Startup creates the lifespan before binding. No
                            # listening sockets exist yet, but the app must exit.
                            await lifespan.shutdown()
            except BaseException as cleanup_error:
                if outcome is None or outcome.error is None:
                    outcome = _ServerOutcome(
                        name=name, started=server.started,
                        clean=server.should_exit, error=cleanup_error,
                    )
                else:
                    outcome = replace(
                        outcome,
                        error=BaseExceptionGroup(
                            "daemon server and shutdown failed",
                            [outcome.error, cleanup_error],
                        ),
                    )
            finally:
                # Delivery below can be cancelled by a sibling's outcome.
                # Record the result without a checkpoint, after cleanup, so
                # the root can reconcile every failure once it has joined us.
                if outcome is not None:
                    terminal_outcomes.append(outcome)
                completed.set()

    async with outcomes:
        async with anyio.create_task_group() as server_group:
            server_group.start_soon(run_server)
            while not server.started and not completed.is_set():
                await anyio.sleep(0)
            if server.started:
                task_status.started()
            await completed.wait()

        assert outcome is not None
        await outcomes.send(outcome)
        if not outcome.started:
            raise _ServerStoppedBeforeReady(outcome)


async def _start_server_and_report_ready(
    task_group, name, server, events, terminal_outcomes
) -> None:
    """Race one server's readiness handshake against sibling outcomes."""
    async with events:
        try:
            await task_group.start(
                _run_server_child,
                name,
                server,
                events.clone(),
                terminal_outcomes,
            )
        except _ServerStoppedBeforeReady:
            # The child sent its startup outcome before raising this transport
            # sentinel, so readiness must not also be reported.
            return
        await events.send(None)


def _raise_for_server_outcome(outcome: _ServerOutcome) -> None:
    if not outcome.started:
        label = "TCP" if outcome.name == "host" else "control"
        raise RuntimeError(f"{label} server failed to bind") from outcome.error
    if outcome.error is not None:
        raise RuntimeError("daemon server failed") from outcome.error
    if not outcome.clean:
        raise RuntimeError("daemon server stopped unexpectedly")


async def _serve(control_app, socket_path, host_app, serve_cfg, tunnel) -> None:
    import anyio
    import uvicorn

    control = uvicorn.Server(
        uvicorn.Config(control_app, uds=str(socket_path), log_config=None)
    )
    servers = [control]
    control_app.state.set_serve_bound(False)
    stop = anyio.Event()
    first_outcome = None
    terminal_outcomes = []
    failures = []
    try:
        send_outcome, receive_outcome = anyio.create_memory_object_stream(2)
        async with send_outcome, receive_outcome:
            async with anyio.create_task_group() as task_group:
                try:
                    try:
                        await task_group.start(
                            _run_server_child,
                            "control",
                            control,
                            send_outcome.clone(),
                            terminal_outcomes,
                        )
                    except _ServerStoppedBeforeReady as stopped:
                        first_outcome = stopped.outcome

                    if (
                        first_outcome is None
                        and host_app is not None
                        and serve_cfg is not None
                    ):
                        host = uvicorn.Server(
                            uvicorn.Config(
                                host_app,
                                host=serve_cfg["host"],
                                port=int(serve_cfg["port"]),
                                log_config=None,
                            )
                        )
                        servers.append(host)
                        task_group.start_soon(
                            _start_server_and_report_ready,
                            task_group,
                            "host",
                            host,
                            send_outcome.clone(),
                            terminal_outcomes,
                        )
                        host_start = await receive_outcome.receive()
                        if host_start is None:
                            control_app.state.set_serve_bound(True)
                        else:
                            first_outcome = host_start

                    if first_outcome is None:
                        _install_stop_handlers(stop, servers)
                        if tunnel is not None:
                            await task_group.start(_tunnel_loop, tunnel, stop)
                        first_outcome = await receive_outcome.receive()
                finally:
                    control_app.state.set_serve_bound(False)
                    for server in servers:
                        server.should_exit = True
                    stop.set()
                    task_group.cancel_scope.cancel()
    except BaseException as error:
        failures.append(error)
    finally:
        control_app.state.set_serve_bound(False)
        # Joined BEFORE the tunnel is torn down, and torn down before `_run`
        # writes its clean-stop entry: a tick still running while `stop()`
        # signals the ssh child could spawn a replacement nothing then owns,
        # and that orphan holds the subdomain against the next start.
        stop.set()
        if tunnel is not None:
            try:
                tunnel.stop()
            except BaseException as error:
                failures.append(error)
    for outcome in terminal_outcomes:
        try:
            _raise_for_server_outcome(outcome)
        except RuntimeError as error:
            failures.append(error)
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("daemon failures", failures)


def _install_stop_handlers(stop, servers) -> None:
    """One Event for the whole process, set by SIGTERM/SIGINT.

    Uvicorn's own handlers only set `should_exit` on the server that installed
    them, so nothing else in the process would ever learn a stop was requested.

    Uvicorn captures signals from inside `serve()`, and `add_signal_handler`
    also replaces the prior handler. The readiness handshakes mean ours is
    installed after every server has entered `serve()` and marked itself
    started, so ours wins on the asyncio backend. Where the platform refuses
    the handler, uvicorn's own handler still ends a server and the first
    completion outcome drives the same shared shutdown path."""
    import asyncio
    import signal

    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        stop.set()
        for server in servers:
            server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError, RuntimeError, ValueError:
            # Non-POSIX, or not the main thread (tests). Uvicorn's own handlers
            # plus the shutdown path still stop everything.
            log.debug("no asyncio signal handler available for %s", sig)


async def _tunnel_loop(
    tunnel,
    stop,
    *,
    task_status: anyio.abc.TaskStatus[None],
) -> None:
    """`tick(); sleep(interval)` until the shared stop Event says otherwise.

    The tick has its own one-token offload capacity because it BLOCKS: a
    registration waits out an HTTP timeout, an auto-reidentify runs `ssh-keygen`,
    the orphan sweep to `ps`, and a respawn opens a `Popen` — any of them on the
    loop thread would stall both HTTP servers. A tick never raises by contract;
    if one ever does it must not end the loop, because the tunnel is the half of
    the daemon that recovers by retrying."""
    import anyio

    from mship.core.async_runtime import run_sync
    from mship.core.daemon.host_tunnel import TICK_INTERVAL_S

    # Readiness means the local loop can supervise reconnects; a relay outage
    # must never hold daemon startup behind network registration/read-back.
    task_status.started()
    while not stop.is_set():
        try:
            # Cancellation joins the bounded tick before the root can stop
            # its supervisor. Abandoning this worker could orphan a respawn.
            await run_sync("tunnel", tunnel.tick)
        except Exception:
            log.exception("tunnel tick failed")
        with anyio.move_on_after(TICK_INTERVAL_S):
            await stop.wait()


def _configure_logging(home: Path) -> None:
    log_dir = paths.daemon_log_dir(home)
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(
        log_dir / "daemon.log", maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
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
    # the rollover is never silent.
    changed = rotate_launchd_captures(paths.daemon_log_dir(home))
    _configure_logging(home)
    for name in changed:
        log.warning(
            "rolled over oversized launchd capture %s (>%d bytes)",
            name,
            LAUNCHD_CAPTURE_MAX_BYTES,
        )
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
        version,
        socket_path,
        os.getpid(),
        len(entries),
    )
    relay_cfg = _relay_config(home)
    # AFTER the lease is won: a tunnel dialed by a process that then stands down
    # for the incumbent would fork an ssh child onto a subdomain it is about to
    # abandon. Constructing one dials nothing — only `tick()` does — so it is
    # safe to build here, and both HTTP apps need its runtime identity/state.
    tunnel_state = None
    try:
        tunnel = _build_tunnel(home, relay_cfg, serve_cfg)
    except Exception as exc:
        log.exception("relay tunnel unavailable — daemon continues without one")
        tunnel = None
        failed_tunnel_state = {
            "state": "error",
            "subdomain": None,
            "public_url": None,
            "restarts": 0,
            "last_error": f"relay tunnel initialization failed: {exc}",
            "clock_skew_seconds": None,
        }

        def tunnel_state():
            return failed_tunnel_state.copy()
    else:
        if tunnel is not None:
            log.info("relay tunnel enabled — dialing %s", tunnel.public_url)
            tunnel_state = tunnel.snapshot

    host_app = None
    if serve_cfg is not None:
        from mship.core.daemon.host_app import (
            create_host_app,
            ensure_host_token,
            load_gh_app_credentials,
        )
        from mship.core.daemon.host_auth import RefreshStore
        from mship.core.daemon.host_token import issue_host_token, verify_host_token
        from mship.core.relay.host_contract import HOST_TOKEN_TTL_S

        if tunnel is not None:

            def current_host_id():
                return tunnel.host_id

            def current_instance_id():
                return tunnel.instance_id

        else:
            from mship.core.daemon.identity import (
                ensure_host_identity,
                machine_fingerprint,
                mint_instance_id,
            )

            identity = ensure_host_identity(home, fingerprint=machine_fingerprint())
            process_instance_id = mint_instance_id()

            def current_host_id():
                return identity.host_id

            def current_instance_id():
                return process_instance_id

        refresh_store = RefreshStore(home)

        def exchange_refresh(credential: str):
            grant = refresh_store.verify_refresh(credential)
            if grant is None or grant.host_id != current_host_id():
                return None
            return issue_host_token(home), HOST_TOKEN_TTL_S

        gh_app_id, gh_app_key = load_gh_app_credentials(home, env=env)
        host_app = create_host_app(
            store,
            auth_token=ensure_host_token(home, env=env),
            verify_bearer=lambda presented: (
                verify_host_token(home, presented) is not None
            ),
            relay_domain=relay_cfg.host if relay_cfg is not None else None,
            exchange_refresh=exchange_refresh,
            host_id=current_host_id,
            instance_id=current_instance_id,
            host_state=tunnel_state,
            rescan=rescan,
            gh_app_id=gh_app_id,
            gh_app_key=gh_app_key,
        )
    app = create_control_app(
        started_at=started_at,
        version=version,
        socket_path=str(socket_path),
        store=store,
        rescan=rescan,
        serve_bound=host_app is not None,
        tunnel=tunnel,
        tunnel_state=tunnel_state,
        after_rescan=(
            host_app.state.drop_stale_subapps if host_app is not None else None
        ),
    )
    _serve_forever(app, socket_path, host_app, serve_cfg, tunnel)
    history.append_clean_stop(
        paths.start_history_path(home), datetime.now(timezone.utc)
    )
    log.info("mshipd stopped cleanly")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    sys.exit(main())

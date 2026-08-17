"""`mship workspace` — override/inspection controls for the daemon's workspace
registry (#472). Discovery is the normal onboarding path; these commands are
the fallback: list what was discovered, add a path discovery can't reach, mint
a fresh id for a duplicate-identity copy, ignore/remove noise, force a rescan.

Like `cli/daemon.py`, `get_container` is accepted for the house register shape
but never resolved into a workspace — the registry is per-host state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import typer

from mship.cli.output import Output
from mship.core.daemon.lease import read_lease_record
from mship.core.daemon.paths import lease_path, registry_path
from mship.core.daemon.registry import (
    RegistryStore,
    load_daemon_config,
    mint_workspace_id,
    reconcile,
    _write_id_file,
)


def _store(home: Path) -> RegistryStore:
    return RegistryStore(registry_path(home))


def register(parent: typer.Typer, get_container):
    ws_app = typer.Typer(
        name="workspace",
        help="Inspect/override the daemon's discovered-workspace registry.",
        no_args_is_help=True,
    )

    @ws_app.command("list")
    def list_():
        """List registry entries (all states, including degraded/missing)."""
        out = Output()
        entries = _store(Path.home()).load().entries
        out.json({
            "workspaces": [
                {"id": e.id, "name": e.name, "path": e.path, "state": e.state,
                 "detail": e.detail, "origin": e.origin, "ignored": e.ignored}
                for e in entries
            ]
        })

    @ws_app.command("add")
    def add(path: str = typer.Argument(..., help="Workspace directory (contains mothership.yaml).")):
        """Manually register a workspace; on a duplicate-identity copy this
        mints a fresh id and rewrites the copy's id file."""
        from mship.core.daemon.discovery import _is_task_worktree, _materialize

        out = Output()
        ws_dir = Path(path).resolve()
        if not (ws_dir / "mothership.yaml").is_file():
            out.error(f"no mothership.yaml at {ws_dir}")
            raise typer.Exit(1)
        if _is_task_worktree(ws_dir):
            out.error(f"{ws_dir} is a task worktree, not an independent workspace")
            raise typer.Exit(1)
        cand = _materialize(ws_dir)
        if not cand.healthy:
            out.error(f"workspace validation failed: {cand.detail}")
            raise typer.Exit(1)
        home = Path.home()
        store = _store(home)
        now = datetime.now(timezone.utc)

        def apply(state):
            from mship.core.daemon.registry import WorkspaceEntry, _read_id_file

            existing_by_path = next((e for e in state.entries if e.path == str(ws_dir)), None)
            if existing_by_path is not None and "duplicate-identity" not in existing_by_path.detail:
                existing_by_path.origin = "manual"
                existing_by_path.ignored = False
                return
            # Duplicate-identity copy (or fresh manual add): mint a new id and
            # claim the directory with it.
            new_id = mint_workspace_id(now)
            wrote = _write_id_file(ws_dir, new_id)
            state.entries = [e for e in state.entries if e.path != str(ws_dir)]
            state.entries.append(WorkspaceEntry(
                id=new_id, name=cand.name, path=str(ws_dir),
                config_path=str(cand.config_path), origin="manual",
                identity_source="idfile" if wrote else "registry-only",
                repos=cand.repos, runtime=cand.runtime, runner=cand.runner,
                first_seen=now, last_seen=now,
            ))

        store.mutate(apply)
        out.print(f"registered {ws_dir}")

    @ws_app.command("remove")
    def remove(workspace_id: str):
        out = Output()

        def apply(state):
            before = len(state.entries)
            state.entries = [e for e in state.entries if e.id != workspace_id]
            if len(state.entries) == before:
                raise KeyError(workspace_id)

        try:
            _store(Path.home()).mutate(apply)
        except KeyError:
            out.error(f"unknown workspace id {workspace_id!r}")
            raise typer.Exit(1)
        out.print(f"removed {workspace_id}")

    @ws_app.command("ignore")
    def ignore(workspace_id: str):
        out = Output()

        def apply(state):
            for e in state.entries:
                if e.id == workspace_id:
                    e.ignored = True
                    return
            raise KeyError(workspace_id)

        try:
            _store(Path.home()).mutate(apply)
        except KeyError:
            out.error(f"unknown workspace id {workspace_id!r}")
            raise typer.Exit(1)
        out.print(f"ignored {workspace_id}")

    @ws_app.command("refresh")
    def refresh():
        """Rescan scan roots. Pokes the live daemon when one holds the lease
        (so its serve cache refreshes too); otherwise reconciles the store
        directly (safe against a later daemon by flock)."""
        from mship.core.daemon.control import probe_control_socket
        from mship.core.daemon.discovery import scan_roots

        out = Output()
        home = Path.home()
        record = read_lease_record(lease_path(home))
        if record is not None and record.socket_path:
            payload = _poke_daemon_refresh(record.socket_path)
            if payload is not None:
                out.print(f"daemon rescanned: {payload.get('workspaces')} workspace(s)")
                return
        cfg = load_daemon_config(home)
        state = reconcile(_store(home), scan_roots(cfg), datetime.now(timezone.utc))
        out.print(f"rescanned directly: {len(state.entries)} workspace(s)")

    parent.add_typer(ws_app, rich_help_panel="Runtime")


def _poke_daemon_refresh(socket_path: str) -> dict | None:
    """POST /workspaces/refresh over the control socket. None on any failure
    (daemon dead or pre-#472) — caller falls back to the direct store path."""
    try:
        import httpx

        client = httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path)), timeout=10.0)
        try:
            r = client.post("http://mshipd/workspaces/refresh")
            return r.json() if r.status_code == 200 else None
        finally:
            client.close()
    except Exception:
        return None

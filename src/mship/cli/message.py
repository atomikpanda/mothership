from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer

from mship.core.message_store import MessageStore


def register(parent: typer.Typer, get_container) -> None:
    def _store() -> MessageStore:
        container = get_container()
        return MessageStore(Path(container.state_dir()) / "messages")

    inbox_app = typer.Typer(help="Inspect and wait on the message inbox.")
    parent.add_typer(inbox_app, name="inbox")

    def _print_awaiting() -> None:
        store = _store()
        out = [
            {"id": t.id, "subject": t.subject,
             "pending": (t.messages[-1].text if t.messages else ""),
             "updated_at": t.updated_at.isoformat()}
            for t in store.list() if t.awaiting_reply
        ]
        if sys.stdout.isatty():
            if not out:
                typer.echo("(inbox empty)")
            for o in out:
                typer.echo(f"{o['id']}  {o['subject']}\n  > {o['pending']}")
        else:
            typer.echo(json.dumps(out))

    @inbox_app.callback(invoke_without_command=True)
    def inbox(ctx: typer.Context) -> None:
        """List threads awaiting an agent reply (latest message is from a human)."""
        if ctx.invoked_subcommand is None:
            _print_awaiting()

    @inbox_app.command("wait")
    def inbox_wait(
        since: str = typer.Option(None, "--since", help="ISO timestamp; only messages after it count (default: now)."),
        timeout: float = typer.Option(50.0, "--timeout", help="Max seconds to block before returning timed_out."),
    ) -> None:
        """Block until a new awaiting (human) message arrives, or timeout. JSON only."""
        from mship.core.message_wait import wait_for_change
        store = _store()
        if since:
            try:
                since_dt = datetime.fromisoformat(since)
            except ValueError:
                typer.echo(f"invalid --since value: {since!r}", err=True)
                raise typer.Exit(2)
        else:
            since_dt = datetime.now(timezone.utc)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
        res = wait_for_change(
            store.list, since_dt, timeout,
            predicate=lambda t: t.awaiting_reply,
        )
        out = {
            "threads": [
                {"id": t.id, "subject": t.subject,
                 "pending": (t.messages[-1].text if t.messages else ""),
                 "updated_at": t.updated_at.isoformat()}
                for t in res.threads
            ],
            "cursor": res.cursor.isoformat(),
            "timed_out": res.timed_out,
        }
        typer.echo(json.dumps(out))

    @parent.command()
    def reply(thread_id: str, text: str) -> None:
        """Post an agent reply to a thread."""
        store = _store()
        try:
            store.append(thread_id, "agent", text, datetime.now(timezone.utc))
        except KeyError:
            typer.echo(f"no thread {thread_id!r}", err=True)
            raise typer.Exit(1)
        typer.echo(f"replied to {thread_id}")

    @parent.command()
    def messages(thread_id: str) -> None:
        """Print a thread's conversation in order."""
        store = _store()
        t = store.get(thread_id)
        if t is None:
            typer.echo(f"no thread {thread_id!r}", err=True)
            raise typer.Exit(1)
        if sys.stdout.isatty():
            for m in t.messages:
                typer.echo(f"[{m.role}] {m.text}")
        else:
            typer.echo(t.model_dump_json())

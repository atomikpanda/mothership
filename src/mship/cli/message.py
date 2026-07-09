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
        import os
        from mship.core.message_wait import wait_for_change
        from mship.core.inbox_lease import InboxLease
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

        # One-listener lease: if another agent in this workspace already holds it,
        # stand down instead of double-draining the shared mailbox (every armed
        # listener would otherwise answer the same message). The caller treats a
        # `skipped_duplicate_listener` result as "don't re-arm". See core/inbox_lease.
        pid = os.getpid()
        lease = InboxLease(Path(get_container().state_dir()) / "inbox-listener.lock")
        holder = lease.try_acquire(pid, datetime.now(timezone.utc))
        if holder is not None:
            typer.echo(json.dumps({
                "threads": [], "cursor": since_dt.isoformat(), "timed_out": False,
                "skipped_duplicate_listener": True, "holder_pid": holder.pid,
            }))
            return

        def _load_and_heartbeat():
            # Refresh the lease each poll so a concurrent reclaim can't steal it
            # mid-wait; the poll loop already ticks ~1s, so no extra thread needed.
            lease.refresh(pid, datetime.now(timezone.utc))
            return store.list()

        try:
            res = wait_for_change(
                _load_and_heartbeat, since_dt, timeout,
                predicate=lambda t: t.awaiting_reply or t.awaiting_agent_event,
            )
        finally:
            lease.release(pid)
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
    def reply(
        thread_id: str,
        text: str,
        needs_you: bool = typer.Option(
            False, "--needs-you",
            help="Mark this reply as needing the operator's action "
                 "(surfaces as a Home action card in Ground Control).",
        ),
    ) -> None:
        """Post an agent reply to a thread."""
        store = _store()
        try:
            store.append(thread_id, "agent", text, datetime.now(timezone.utc),
                         kind="needs_you" if needs_you else "note")
        except KeyError:
            typer.echo(f"no thread {thread_id!r}", err=True)
            raise typer.Exit(1)
        typer.echo(f"replied to {thread_id}")

    @parent.command()
    def ask(
        thread_id: str,
        question: str,
        option: list[str] = typer.Option(..., "--option", help="A choice (repeat for each; >=2)."),
        recommend: int = typer.Option(None, "--recommend", help="0-based index of the recommended option."),
        no_free_text: bool = typer.Option(False, "--no-free-text", help="Disallow a free-text reply."),
        multi: bool = typer.Option(False, "--multi", help="Allow selecting more than one option."),
    ) -> None:
        """Post an agent DECISION: a question + tappable options (surfaces as a decision card)."""
        from mship.core.message import DecisionPayload
        if len(option) < 2:
            typer.echo("a decision needs at least two --option values", err=True)
            raise typer.Exit(2)
        if recommend is not None and not (0 <= recommend < len(option)):
            typer.echo(f"--recommend {recommend} out of range for {len(option)} options", err=True)
            raise typer.Exit(2)
        store = _store()
        try:
            store.append(thread_id, "agent", question, datetime.now(timezone.utc),
                         kind="decision",
                         decision=DecisionPayload(options=option, recommended=recommend,
                                                  allow_free_text=not no_free_text,
                                                  multi=multi))
        except KeyError:
            typer.echo(f"no thread {thread_id!r}", err=True)
            raise typer.Exit(1)
        typer.echo(f"asked {thread_id}: {len(option)} options")

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

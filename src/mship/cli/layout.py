import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

import typer

def serve_cli_args(
    *, host: Optional[str], port: Optional[int], relay: bool, relay_host: Optional[str]
) -> list[str]:
    """Map `mship layout launch` serve options to `mship serve` CLI args, in a stable order."""
    args: list[str] = []
    if host is not None:
        args += ["--host", host]
    if port is not None:
        args += ["--port", str(port)]
    if relay:
        args += ["--relay"]
    if relay_host is not None:
        args += ["--relay-host", relay_host]
    return args


def _kdl_quote(s: str) -> str:
    """Quote a string as a KDL string literal, escaping backslash and double-quote
    so user-supplied values (e.g. --host) can't produce malformed KDL."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True)
class ViewPaneSpec:
    """One right-side stack member: a display name + the `mship <args>` it runs
    (always a `mship view … --follow` command)."""
    name: str
    view_args: list[str]


# The right-side stack is ONE fixed rich set, the same regardless of the focused
# item's phase (operator decision, option A). ac4's "dynamic per phase" is met by
# every member running `--follow`: each re-scopes to the focused item and renders
# phase-appropriate content (or its own empty/hint state). The Item pane carries the
# item/PR/checks composite. No phase-conditional membership, no rebuild on switch.
_COCKPIT_VIEW_SPECS = [
    ViewPaneSpec("Spec", ["view", "spec", "--follow"]),
    ViewPaneSpec("Diff", ["view", "diff", "--follow"]),
    ViewPaneSpec("Journal", ["view", "journal", "--follow"]),
    ViewPaneSpec("Status", ["view", "status", "--follow"]),
    ViewPaneSpec("Item", ["view", "item", "--follow"]),
]


def cockpit_view_specs() -> list[ViewPaneSpec]:
    """The fixed rich set of right-side stack members (ac4). Always the same — the
    panes follow focus; membership never depends on phase."""
    return list(_COCKPIT_VIEW_SPECS)


_FRAME = """\
    default_tab_template {
        pane size=1 borderless=true {
            plugin location="zellij:tab-bar"
        }
        children
        pane size=2 borderless=true {
            plugin location="zellij:status-bar"
        }
    }
"""

_OVERVIEW_TAB = """\
    tab name="Overview" {
        pane split_direction="vertical" {
            pane size="50%" name="Queue" command="mship" close_on_exit=false { args "view" "queue"; }
            pane size="50%" name="Items" command="mship" close_on_exit=false { args "view" "items"; }
        }
    }
"""


def _cockpit_agent_pane(chat_command: str | None) -> str:
    """The fixed left Agent pane — a bare shell (operator's session) by default, or a
    configured command. size 40%, focus=true, sibling of (NOT inside) the stack."""
    if chat_command is None:
        return '            pane size="40%" name="Agent" focus=true\n'
    cmd = _kdl_quote(chat_command)
    return ('            pane size="40%" name="Agent" focus=true command="sh" close_on_exit=false '
            f'{{ args "-c" {cmd}; }}\n')


def _view_pane_kdl(spec: "ViewPaneSpec") -> str:
    args = " ".join(_kdl_quote(t) for t in spec.view_args)
    return (f'                pane name="{spec.name}" command="mship" close_on_exit=false '
            f'{{ args {args}; }}\n')


def _cockpit_tab(chat_command: str | None) -> str:
    panes = "".join(_view_pane_kdl(s) for s in cockpit_view_specs())
    return ('    tab name="Cockpit" focus=true {\n'
            '        pane split_direction="vertical" {\n'
            + _cockpit_agent_pane(chat_command)
            + '            pane stacked=true {\n'
            + panes
            + '            }\n'
            + '        }\n'
            + '    }\n')


def render_cockpit_layout(*, chat_command: str | None = None) -> str:
    """The v2 cockpit layout document (ac3): the frame, an Overview launchpad tab
    (queue + items), and a single Cockpit tab = fixed Agent pane beside a stacked
    group of the fixed rich set of `mship view … --follow` panes (Spec, Diff,
    Journal, Status, Item). Membership never depends on phase; the panes follow
    focus."""
    return ("layout {\n"
            + _FRAME + "\n"
            + _OVERVIEW_TAB + "\n"
            + _cockpit_tab(chat_command)
            + "}\n")


_TEMPLATE = render_cockpit_layout()

# Serve splices a Serve tab before the final layout-closing brace. The only
# column-0 brace is the layout close, so `\n}\n` locates it uniquely.
_close_idx = _TEMPLATE.rindex("\n}\n") + 1
_LAYOUT_HEAD = _TEMPLATE[:_close_idx]
_BASE_TABS = ""
_LAYOUT_TAIL = _TEMPLATE[_close_idx:]


def resolve_chat_command(explicit: str | None, env: Mapping[str, str]) -> str | None:
    """Configurable agent/chat command. None -> a bare pane = the operator's shell
    in the tab cwd (mship does NOT hardcode a specific agent)."""
    if explicit:
        return explicit
    return env.get("MSHIP_CHAT_COMMAND") or None


def resolve_focus_target(container, item_id: str):
    """Resolve (WorkItemSummary, primary task_slug|None, worktree cwd) for an item,
    or None if the id is unknown. Reuses load_workitem_index + dispatch.resolve_repo
    (active_repo > sole worktree). Falls back to the workspace root when the item has
    no usable worktree yet."""
    from mship.cli.view._workitems import load_workitem_index
    from mship.core.dispatch import resolve_repo

    summary = next((s for s in load_workitem_index(container) if s.id == item_id), None)
    if summary is None:
        return None

    state = container.state_manager().load()
    task_slug: str | None = None
    worktree: Path | None = None
    for slug in summary.task_slugs:
        task = state.tasks.get(slug)
        if task is None:
            continue
        task_slug = task_slug or slug
        try:
            repo = resolve_repo(task, None)
        except ValueError:
            continue
        worktree = Path(task.worktrees[repo])
        task_slug = slug
        break

    if worktree is None:
        worktree = Path(container.config_path()).parent
    return summary, task_slug, worktree


def _serve_tab(serve_args: list[str]) -> str:
    """The Serve tab KDL block: one pane running `mship serve <serve_args>`."""
    tokens = " ".join(_kdl_quote(a) for a in ["serve", *serve_args])
    return (
        '\n    tab name="Serve" {\n'
        '        pane name="Serve" command="mship" close_on_exit=false { args '
        + tokens
        + "; }\n"
        "    }\n"
    )


def render_serve_layout(serve_args: list[str]) -> str:
    """The serve layout: the normal base tabs plus a Serve tab, as a full KDL document."""
    return _LAYOUT_HEAD + _BASE_TABS + _serve_tab(serve_args) + _LAYOUT_TAIL


def _target_path() -> Path:
    return Path.home() / ".config" / "zellij" / "layouts" / "mothership.kdl"


def _serve_target_path() -> Path:
    return Path.home() / ".config" / "zellij" / "layouts" / "mothership-serve.kdl"


def _launch_layout_path() -> Path:
    """Per-PROCESS path for the launch-time effective cockpit layout, keyed by PID.

    Two concurrent `mship layout launch` invocations (different --chat-command /
    serve args) are different processes with different PIDs, so they write to
    DIFFERENT files and can't race on a shared path before zellij reads it. `os.execvp`
    replaces the process so no cleanup runs after it, but keying on the PID (rather
    than a unique tempfile) keeps the set of orphaned .kdl files bounded — PIDs
    recycle and each launch overwrites its own file.
    """
    return Path.home() / ".config" / "zellij" / f"mothership-launch-{os.getpid()}.kdl"


def _in_zellij() -> bool:
    """Seam: are we inside a zellij session? ($ZELLIJ is set by zellij)."""
    return bool(os.environ.get("ZELLIJ"))


def register(app: typer.Typer, get_container):
    layout_app = typer.Typer(name="layout", help="Manage the zellij layout for mothership.", no_args_is_help=True)

    @layout_app.command()
    def init(
        force: bool = typer.Option(False, "--force", help="Overwrite existing layout files."),
    ):
        """Write the mothership zellij layouts (normal + serve) to ~/.config/zellij/layouts/."""
        normal = _target_path()
        serve = _serve_target_path()
        existing = [p for p in (normal, serve) if p.exists()]
        if existing and not force:
            typer.echo(
                "Error: layout file(s) already exist: "
                + ", ".join(str(p) for p in existing)
                + ". Use --force to overwrite.",
                err=True,
            )
            raise typer.Exit(code=1)
        normal.parent.mkdir(parents=True, exist_ok=True)
        normal.write_text(_TEMPLATE)
        serve.write_text(render_serve_layout([]))
        typer.echo(f"Written: {normal}")
        typer.echo(f"Written: {serve}")

    @layout_app.command()
    def launch(
        serve: bool = typer.Option(False, "--serve", help="Add a Serve tab running `mship serve`."),
        host: Optional[str] = typer.Option(None, "--host", help="serve --host (implies --serve)."),
        port: Optional[int] = typer.Option(None, "--port", help="serve --port (implies --serve)."),
        relay: bool = typer.Option(False, "--relay", help="serve --relay (implies --serve)."),
        relay_host: Optional[str] = typer.Option(None, "--relay-host", help="serve --relay-host (implies --serve)."),
        chat_command: Optional[str] = typer.Option(
            None, "--chat-command", help="Command for the Agent pane. Default: your shell."),
    ):
        """Launch zellij with the v2 cockpit layout (replaces current process).

        Renders an effective cockpit: the Agent pane runs your shell (or
        --chat-command / $MSHIP_CHAT_COMMAND) beside the fixed rich stack of
        `mship view … --follow` panes. With any serve flag, also adds a Serve tab
        running `mship serve`."""
        serve_selected = (
            serve or host is not None or port is not None or relay or relay_host is not None
        )
        kdl = render_cockpit_layout(
            chat_command=resolve_chat_command(chat_command, os.environ),
        )
        if serve_selected:
            args = serve_cli_args(host=host, port=port, relay=relay, relay_host=relay_host)
            # Splice the Serve tab in exactly as render_serve_layout does.
            close_idx = kdl.rindex("\n}\n") + 1
            kdl = kdl[:close_idx] + _serve_tab(args) + kdl[close_idx:]

        path = _launch_layout_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(kdl)
        os.execvp("zellij", ["zellij", "--layout", str(path)])

    @layout_app.command()
    def focus(
        item_id: Optional[str] = typer.Argument(
            None, help="WorkItem id to focus (e.g. wi-...)."),
        show: bool = typer.Option(
            False, "--show", help="Print the current focus and exit."),
    ):
        """Record <id> as the workspace CURRENT FOCUS (cockpit-v2, ac1).

        Writes the focus file — it does NOT open/switch a zellij tab; the cockpit's
        right-side `--follow` views re-scope to the new focus on their next tick.
        `--show` prints the current focus. Works in any pane; when not inside a
        zellij session it still writes the file and just adds an advisory."""
        from mship.core.focus import focus_path, read_focus, write_focus
        from mship.cli.view._workitems import load_workitem_index

        container = get_container()
        state_dir = container.state_dir()

        if show:
            current = read_focus(focus_path(state_dir))
            if current is None:
                typer.echo("No WorkItem focused. Run `mship layout focus <id>`.")
            else:
                typer.echo(f"Focused: {current.work_item_id}")
            return

        if item_id is None:
            typer.echo("Error: provide a WorkItem id, or --show.", err=True)
            raise typer.Exit(code=1)

        summary = next((s for s in load_workitem_index(container) if s.id == item_id), None)
        if summary is None:
            typer.echo(f"Error: unknown work item: {item_id}", err=True)
            raise typer.Exit(code=1)

        write_focus(focus_path(state_dir), item_id)
        if summary.phase == "done":
            typer.echo(f"Focused {item_id} (note: this item is done).")
        else:
            typer.echo(f"Focused {item_id}.")
        if not _in_zellij():
            typer.echo("Not inside a zellij session; run `mship layout launch` "
                       "to open the cockpit. (focus recorded)")

    app.add_typer(layout_app, rich_help_panel="Setup")

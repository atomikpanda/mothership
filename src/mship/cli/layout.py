import os
import subprocess
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

# The tab-bar (top) + status-bar (bottom) frame every tab via default_tab_template.
# A per-item focus tab (render_workitem_layout) is opened with `new-tab --layout`
# from its OWN layout doc, so it must carry the same block or it renders with NO
# tab bar. Sliced verbatim from _TEMPLATE so the two can never drift. (Dead after
# Task 12 removes render_workitem_layout.)
_DEFAULT_TAB_TEMPLATE = _TEMPLATE[
    _TEMPLATE.index("    default_tab_template {") : _TEMPLATE.index('    tab name="Overview"')
]


def tab_name_for(item_id: str) -> str:
    """Deterministic zellij tab name for a WorkItem. The id verbatim: the same
    item always maps to the same tab, so focus reconciles rather than duplicates."""
    return item_id


def decide_focus_action(tab_name: str, existing_tab_names: list[str], *, is_done: bool) -> str:
    """Pure go-vs-create-vs-close decision for `mship layout focus`.
    Returns "close" | "noop" | "go-to" | "create"."""
    exists = tab_name in existing_tab_names
    if is_done:
        return "close" if exists else "noop"
    return "go-to" if exists else "create"


_PHASES = ("Plan", "Dev", "Review", "Run")

_PHASE_FROM_WORKITEM = {
    "inbox": "Plan", "shaping": "Plan", "ready": "Plan",
    "in_flight": "Dev", "review": "Review", "done": "Run",
}


def default_phase_tab(workitem_phase: str) -> str:
    """Map a WorkItem's derived phase to the sub-tab that opens focused."""
    return _PHASE_FROM_WORKITEM.get(workitem_phase, "Plan")


def resolve_chat_command(explicit: str | None, env: Mapping[str, str]) -> str | None:
    """Configurable agent/chat command. None -> a bare pane = the operator's shell
    in the tab cwd (mship does NOT hardcode a specific agent)."""
    if explicit:
        return explicit
    return env.get("MSHIP_CHAT_COMMAND") or None


def _mship_pane(name: str, tokens: list[str]) -> str:
    args = " ".join(_kdl_quote(t) for t in tokens)
    return (f'                pane name="{name}" command="mship" close_on_exit=false '
            f'{{ args {args}; }}\n')


def _phase_panes(phase: str, item_id: str, task_slug: str | None) -> str:
    """The ambient view panes for one phase sub-tab, baking the item/task in. Panes
    that need a task fall back to a Shell pane when the item has no task yet.

    NOTE (deviation): `mship view logs` does not exist — the run/journal view is
    registered as `view journal` (in cli/view/logs.py). The Run phase therefore
    tails `view journal`, not a nonexistent `view logs`."""
    shell = '                pane name="Shell"\n'
    if phase == "Plan":
        return (_mship_pane("Spec", ["view", "spec", "--workitem", item_id, "--watch"])
                + _mship_pane("Item", ["view", "item", item_id]))
    if phase == "Dev":
        if task_slug is None:
            return shell
        return (_mship_pane("Diff", ["view", "diff", "--task", task_slug, "--watch"])
                + _mship_pane("Journal", ["view", "journal", "--task", task_slug, "--watch"]))
    if phase == "Review":
        item = _mship_pane("Item", ["view", "item", item_id])
        if task_slug is None:
            return item
        return _mship_pane("Diff", ["view", "diff", "--task", task_slug, "--watch"]) + item
    if phase == "Run":
        if task_slug is None:
            return shell
        return _mship_pane("Logs", ["view", "journal", "--task", task_slug, "--watch"]) + shell
    return shell


def _agent_pane(chat_command: str | None) -> str:
    if chat_command is None:
        return '            pane name="Agent" focus=true\n'
    cmd = _kdl_quote(chat_command)
    return ('            pane name="Agent" focus=true command="sh" close_on_exit=false '
            f'{{ args "-c" {cmd}; }}\n')


def _editor_pane() -> str:
    return ('            pane name="Editor" command="sh" close_on_exit=false {\n'
            '                args "-c" "${EDITOR:-$(command -v nvim || command -v vim || command -v vi)} ."\n'
            '            }\n')


def _phase_swap(phase: str, item_id: str, task_slug: str | None,
                chat_command: str | None) -> str:
    return (f'    swap_tiled_layout name="{phase}" {{\n'
            '        tab {\n'
            '            pane split_direction="vertical" {\n'
            + _agent_pane(chat_command)
            + '                pane split_direction="horizontal" size="50%" {\n'
            + _phase_panes(phase, item_id, task_slug)
            + '                }\n'
            + '            }\n'
            + _editor_pane()
            + '        }\n'
            '    }\n')


def render_workitem_layout(
    *, name: str, worktree: str, item_id: str, task_slug: str | None,
    chat_command: str | None, default_phase: str,
) -> str:
    """A per-WorkItem tab KDL: chat-first Agent pane + Editor pane, all cd'd to the
    worktree, with Plan/Dev/Review/Run phase sub-tabs (zellij swap layouts) whose
    panes are the shipped `mship view` commands baked to this item/task. Includes the
    same default_tab_template as the base layout so the focus tab shows the tab-bar /
    status-bar. Reuses _kdl_quote so paths/commands can't break out of the KDL string."""
    base_phase = default_phase if default_phase in _PHASES else "Plan"
    parts = [f'layout {{\n    cwd {_kdl_quote(worktree)}\n\n',
             _DEFAULT_TAB_TEMPLATE,
             f'    tab name="{name}" focus=true {{\n',
             '        pane split_direction="vertical" {\n',
             _agent_pane(chat_command),
             '            pane split_direction="horizontal" size="50%" {\n',
             _phase_panes(base_phase, item_id, task_slug),
             '            }\n',
             '        }\n',
             _editor_pane(),
             '    }\n\n']
    for phase in _PHASES:
        parts.append(_phase_swap(phase, item_id, task_slug, chat_command))
    parts.append('}\n')
    return "".join(parts)


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


def _serve_launch_path() -> Path:
    """Deterministic per-user path for the launch-time effective serve layout.

    A fixed path (overwritten each launch, outside the layouts/ picker dir) rather
    than a unique tempfile: `os.execvp` replaces the process, so no cleanup runs
    after it — a unique temp would orphan one .kdl per launch. Overwriting one file
    keeps it bounded.
    """
    return Path.home() / ".config" / "zellij" / "mothership-serve-launch.kdl"


def _layout_cache_dir() -> Path:
    """mship-owned cache dir for per-WorkItem layout KDL files that `zellij action
    new-tab --layout <name> --layout-dir <dir>` resolves by name."""
    return Path.home() / ".cache" / "mship" / "layouts"


def write_workitem_layout_file(item_id: str, kdl: str) -> tuple[str, Path]:
    """Write a WorkItem's layout KDL to a stable cache file and return
    (layout_name, layout_dir) for `new-tab --layout <name> --layout-dir <dir>`.

    Delivering the KDL via a file (resolved by name) rather than `--layout-string`
    keeps this portable to older zellij that lack `--layout-string` (the operator's
    version). The path is stable and keyed by item_id (overwritten on re-focus) — NOT
    a delete-on-close tempfile — so zellij's server can read it after we return
    without racing a cleanup delete."""
    cache_dir = _layout_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{item_id}.kdl").write_text(kdl)
    return item_id, cache_dir


def _in_zellij() -> bool:
    """Seam: are we inside a zellij session? ($ZELLIJ is set by zellij)."""
    return bool(os.environ.get("ZELLIJ"))


def _query_tab_names() -> list[str] | None:
    """Seam: the current session's tab names via `zellij action query-tab-names`.
    Returns None if the query itself FAILED, so callers don't mistake a failure
    for an empty session (which would create duplicate tabs / report wrong state)."""
    out = subprocess.run(["zellij", "action", "query-tab-names"],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0:
        return None
    return [line for line in out.stdout.splitlines() if line.strip()]


def _run_zellij_action(args: list[str]) -> bool:
    """Seam: run one `zellij action <args>`; returns True on success (exit 0), so
    callers can report real outcomes and never `close-tab` after a failed go-to."""
    return subprocess.run(["zellij", "action", *args], check=False).returncode == 0


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
        serve: bool = typer.Option(False, "--serve", help="Open the serve layout (adds a Serve tab running `mship serve`)."),
        host: Optional[str] = typer.Option(None, "--host", help="serve --host (implies --serve)."),
        port: Optional[int] = typer.Option(None, "--port", help="serve --port (implies --serve)."),
        relay: bool = typer.Option(False, "--relay", help="serve --relay (implies --serve). Collides with a separately-running serve."),
        relay_host: Optional[str] = typer.Option(None, "--relay-host", help="serve --relay-host (implies --serve)."),
    ):
        """Launch zellij with the mothership layout (replaces current process).

        With --serve (or any serve flag) launches the serve layout — the normal
        tabs plus a Serve tab running `mship serve <flags>`. WARNING: starting a
        serve here collides with a separately-running `mship serve` (same relay
        subdomain / bind), so use it only when no standalone serve is up.
        """
        serve_selected = (
            serve or host is not None or port is not None or relay or relay_host is not None
        )
        if not serve_selected:
            os.execvp("zellij", ["zellij", "--layout", "mothership"])
            return
        args = serve_cli_args(host=host, port=port, relay=relay, relay_host=relay_host)
        kdl = render_serve_layout(args)
        path = _serve_launch_path()
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

    @layout_app.command()
    def close(
        item_id: str = typer.Argument(..., help="WorkItem id whose tab to close."),
    ):
        """Explicitly close a WorkItem's zellij tab so tabs don't accumulate (AC7)."""
        if not _in_zellij():
            typer.echo("Not inside a zellij session ($ZELLIJ unset). (no-op)")
            return
        name = tab_name_for(item_id)
        existing = _query_tab_names()
        if existing is None:
            typer.echo("Error: could not query zellij tabs.", err=True)
            raise typer.Exit(code=1)
        if name not in existing:
            typer.echo(f"No open tab for {item_id}.")
            return
        # Close only after a successful go-to, so a failed go-to can't close the
        # active tab (Overview or an unrelated item) instead of the target.
        if _run_zellij_action(["go-to-tab-name", name]) and _run_zellij_action(["close-tab"]):
            typer.echo(f"Closed tab for {item_id}.")
            return
        typer.echo(f"Error: could not close tab for {item_id}.", err=True)
        raise typer.Exit(code=1)

    app.add_typer(layout_app, rich_help_panel="Setup")

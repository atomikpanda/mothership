import os
from pathlib import Path
from typing import Mapping, Optional

import typer

_TEMPLATE = """\
layout {
    default_tab_template {
        pane size=1 borderless=true {
            plugin location="zellij:tab-bar"
        }
        children
        pane size=2 borderless=true {
            plugin location="zellij:status-bar"
        }
    }

    tab name="Plan" focus=true {
        pane split_direction="vertical" {
            pane size="50%" name="Agent"
            pane split_direction="horizontal" size="50%" {
                pane name="Specs" command="mship" close_on_exit=false { args "view" "spec" "--watch"; }
                pane name="Status" command="mship" close_on_exit=false { args "view" "status" "--watch"; }
            }
        }
    }

    tab name="Dev" {
        pane split_direction="vertical" {
            pane size="60%" name="Editor" command="sh" close_on_exit=false {
                args "-c" "${EDITOR:-$(command -v nvim || command -v vim || command -v vi)} ."
            }
            pane split_direction="horizontal" size="40%" {
                pane name="Journal" command="mship" close_on_exit=false { args "view" "journal" "--watch"; }
                pane name="Status" command="mship" close_on_exit=false { args "view" "status" "--watch"; }
            }
        }
    }

    tab name="Review" {
        pane split_direction="vertical" {
            pane size="70%" name="Diff" command="mship" close_on_exit=false { args "view" "diff" "--watch"; }
            pane size="30%" split_direction="horizontal" {
                pane name="Shell"
                pane name="Journal" command="mship" close_on_exit=false { args "view" "journal" "--watch"; }
            }
        }
    }

    tab name="Run" {
        pane split_direction="vertical" {
            pane size="60%" name="Shell"
            pane split_direction="horizontal" size="40%" {
                pane name="Journal" command="mship" close_on_exit=false { args "view" "journal" "--watch"; }
                pane name="Status" command="mship" close_on_exit=false { args "view" "status" "--watch"; }
            }
        }
    }
}
"""

# Composable parts, sliced from _TEMPLATE at stable markers so the normal layout
# is reconstructed byte-for-byte (_LAYOUT_HEAD + _BASE_TABS + _LAYOUT_TAIL == _TEMPLATE)
# while the serve layout can reuse the base tabs and append a Serve tab. The final
# layout-closing "}" is the only brace at column 0, so `\n}\n` locates it uniquely.
_plan_idx = _TEMPLATE.index('    tab name="Plan"')
_close_idx = _TEMPLATE.rindex("\n}\n") + 1
_LAYOUT_HEAD = _TEMPLATE[:_plan_idx]
_BASE_TABS = _TEMPLATE[_plan_idx:_close_idx]
_LAYOUT_TAIL = _TEMPLATE[_close_idx:]


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
    panes are the shipped `mship view` commands baked to this item/task. Reuses
    _kdl_quote so paths/commands can't break out of the KDL string."""
    base_phase = default_phase if default_phase in _PHASES else "Plan"
    parts = [f'layout {{\n    cwd {_kdl_quote(worktree)}\n\n',
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

    app.add_typer(layout_app, rich_help_panel="Setup")

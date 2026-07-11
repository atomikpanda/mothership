import typer

from mship.container import Container

app = typer.Typer(
    name="mship",
    help=(
        "Cross-repo workflow engine. "
        "Task-scoped commands resolve their target via --task flag -> MSHIP_TASK env -> cwd.\n\n"
        "Output flags (place before the command, e.g. `mship --json status`):\n"
        "  --json      force JSON output (implies --no-color)\n"
        "  --quiet/-q  suppress advisory warnings + progress on stderr\n"
        "  --no-color  strip ANSI color\n"
        "Precedence: CLI flag > env var (MSHIP_JSON / MSHIP_QUIET / NO_COLOR) > TTY auto-detection."
    ),
    no_args_is_help=True,
)

container = Container()


@app.callback()
def _global_options(
    json: bool = typer.Option(
        False, "--json",
        help="Force JSON output regardless of TTY (implies --no-color). "
             "Overrides MSHIP_JSON.",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress advisory warnings and progress lines on stderr "
             "(errors and exit codes unchanged). Overrides MSHIP_QUIET.",
    ),
    no_color: bool = typer.Option(
        False, "--no-color",
        help="Strip ANSI color from all output (per no-color.org). Overrides NO_COLOR.",
    ),
) -> None:
    """Resolve global output flags before any command runs.

    These are recorded process-globally and consumed by ``mship.cli.output``;
    a flag forces its setting on, while leaving a flag off defers to the env var
    then TTY auto-detection (so plain ``mship status | jq`` still yields JSON).
    """
    from mship.cli.output import configure_output

    configure_output(
        json=True if json else None,
        quiet=True if quiet else None,
        no_color=True if no_color else None,
    )


def _resolve_state_dir(config_path):
    """Get the workspace state dir, anchored to main repo if in a git worktree."""
    import os
    import subprocess
    from pathlib import Path

    config_path = Path(config_path)
    try:
        # Strip GIT_DIR / GIT_COMMON_DIR so git re-discovers from cwd rather than
        # inheriting a worktree-specific git dir set by a parent git hook process.
        env = {k: v for k, v in os.environ.items()
               if k not in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE")}
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=config_path.parent,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        git_common_dir = Path(result.stdout.strip())
        if not git_common_dir.is_absolute():
            git_common_dir = (config_path.parent / git_common_dir).resolve()
        return git_common_dir.parent / ".mothership"
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return config_path.parent / ".mothership"


def get_container(required: bool = True) -> "Container | None":
    """Lazy container initialization with config discovery.

    `required=True` (default): missing workspace → stderr error + typer.Exit(1).
    `required=False`: missing workspace → return None silently. Used by hook
    commands so they don't spam `No mothership.yaml` warnings from commits
    in non-mship repos. See #86.
    """
    from pathlib import Path
    from mship.core.config import ConfigLoader

    try:
        if not container.config_path.overridden:
            config_path = ConfigLoader.discover(Path.cwd())
            container.config_path.override(config_path)
        if not container.state_dir.overridden:
            config_path = container.config_path()
            state_dir = _resolve_state_dir(config_path)
            container.state_dir.override(state_dir)
    except FileNotFoundError:
        if not required:
            return None
        import sys
        print("Error: No mothership.yaml found in any parent directory", file=sys.stderr)
        raise typer.Exit(code=1)
    return container


# Register command modules
from mship.cli import audit as _audit_mod
from mship.cli import bind as _bind_mod
from mship.cli import capture as _capture_mod
from mship.cli import bootstrap as _bootstrap_mod
from mship.cli import block as _block_mod
from mship.cli import commit as _commit_mod
from mship.cli import context as _context_mod
from mship.cli import debug as _debug_mod
from mship.cli import depends as _depends_mod
from mship.cli import dispatch as _dispatch_mod
from mship.cli import doctor as _doctor_mod
from mship.cli import exec as _exec_mod
from mship.cli import export as _export_mod
from mship.cli import gh as _gh_mod
from mship.cli import init as _init_mod
from mship.cli import internal as _internal_mod
from mship.cli import layout as _layout_mod
from mship.cli import log as _log_mod
from mship.cli import message as _message_mod
from mship.cli import pair as _pair_mod
from mship.cli import phase as _phase_mod
from mship.cli import pr as _pr_mod
from mship.cli import prune as _prune_mod
from mship.cli import reconcile as _reconcile_mod
from mship.cli import relay as _relay_mod
from mship.cli import run_host as _run_host_mod
from mship.cli import skill as _skill_mod
from mship.cli import spec as _spec_mod
from mship.cli import status as _status_mod
from mship.cli import switch as _switch_mod
from mship.cli import serve as _serve_mod
from mship.cli import sync as _sync_mod
from mship.cli import view as _view_mod
from mship.cli import worktree as _worktree_mod
from mship.cli import workitem as _workitem_mod

def _should_silent_exit(argv: list[str]) -> bool:
    """True if argv is invoking an unknown `_`-prefixed internal command.

    Stale git hooks from older mship versions invoke renamed internals like
    `mship _log-commit`. They're wrapped in `|| true` in the hook body, so
    the hook itself is fine with a nonzero exit — but typer prints a 6-line
    usage error, which is noise on every single commit until the user runs
    `mship init --install-hooks`. Swallow it.
    """
    if len(argv) < 2:
        return False
    cmd = argv[1]
    if not cmd.startswith("_"):
        return False
    known = {c.name for c in app.registered_commands if c.name}
    return cmd not in known


def run() -> None:
    """Entry point wrapper — see `_should_silent_exit`."""
    import sys
    if _should_silent_exit(sys.argv):
        sys.exit(0)
    app()


_audit_mod.register(app, get_container)
_bind_mod.register(app, get_container)
_capture_mod.register(app, get_container)
_bootstrap_mod.register(app, get_container)
_block_mod.register(app, get_container)
_commit_mod.register(app, get_container)
_context_mod.register(app, get_container)
_debug_mod.register(app, get_container)
_depends_mod.register(app, get_container)
_dispatch_mod.register(app, get_container)
_doctor_mod.register(app, get_container)
_exec_mod.register(app, get_container)
_export_mod.register(app, get_container)
_gh_mod.register(app, get_container)
_init_mod.register(app, get_container)
_internal_mod.register(app, get_container)
_layout_mod.register(app, get_container)
_log_mod.register(app, get_container)
_message_mod.register(app, get_container)
_pair_mod.register(app, get_container)
_phase_mod.register(app, get_container)
_pr_mod.register(app, get_container)
_prune_mod.register(app, get_container)
_reconcile_mod.register(app, get_container)
_relay_mod.register(app, get_container)
_run_host_mod.register(app, get_container)
_skill_mod.register(app, get_container)
_spec_mod.register(app, get_container)
_status_mod.register(app, get_container)
_switch_mod.register(app, get_container)
_serve_mod.register(app, get_container)
_sync_mod.register(app, get_container)
_view_mod.register(app, get_container)
_worktree_mod.register(app, get_container)
_workitem_mod.register(app, get_container)

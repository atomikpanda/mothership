import typer

from mship.container import Container

app = typer.Typer(name="mship", help="Cross-repo workflow engine")

container = Container()


def _resolve_state_dir(config_path):
    """Get the workspace state dir, anchored to main repo if in a git worktree."""
    import subprocess
    from pathlib import Path

    config_path = Path(config_path)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=config_path.parent,
            capture_output=True,
            text=True,
            check=True,
        )
        git_common_dir = Path(result.stdout.strip())
        if not git_common_dir.is_absolute():
            git_common_dir = (config_path.parent / git_common_dir).resolve()
        return git_common_dir.parent / ".mothership"
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return config_path.parent / ".mothership"


def get_container() -> Container:
    """Lazy container initialization with config discovery."""
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
        import sys
        print("Error: No mothership.yaml found in any parent directory", file=sys.stderr)
        raise typer.Exit(code=1)
    return container


# Register command modules
from mship.cli import status as _status_mod
from mship.cli import phase as _phase_mod
from mship.cli import worktree as _worktree_mod
from mship.cli import exec as _exec_mod
from mship.cli import block as _block_mod
from mship.cli import log as _log_mod
from mship.cli import prune as _prune_mod
from mship.cli import init as _init_mod
from mship.cli import doctor as _doctor_mod
from mship.cli import skill as _skill_mod
from mship.cli import view as _view_mod

_status_mod.register(app, get_container)
_phase_mod.register(app, get_container)
_worktree_mod.register(app, get_container)
_exec_mod.register(app, get_container)
_block_mod.register(app, get_container)
_log_mod.register(app, get_container)
_prune_mod.register(app, get_container)
_init_mod.register(app, get_container)
_doctor_mod.register(app, get_container)
_skill_mod.register(app, get_container)
_view_mod.register(app, get_container)

import typer

from mship.container import Container

app = typer.Typer(name="mship", help="Cross-repo workflow engine")

container = Container()


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
            state_dir = Path(config_path).parent / ".mothership"
            container.state_dir.override(state_dir)
    except FileNotFoundError:
        raise typer.Exit(code=1)
    return container


# Register command modules
from mship.cli import status as _status_mod
from mship.cli import phase as _phase_mod
from mship.cli import worktree as _worktree_mod
from mship.cli import exec as _exec_mod

_status_mod.register(app, get_container)
_phase_mod.register(app, get_container)
_worktree_mod.register(app, get_container)
_exec_mod.register(app, get_container)

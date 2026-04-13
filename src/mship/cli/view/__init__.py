import typer

app = typer.Typer(name="view", help="Read-only live views for tmux/zellij panes")


def register(parent: typer.Typer, get_container):
    from mship.cli.view import status as _status
    from mship.cli.view import logs as _logs
    from mship.cli.view import diff as _diff
    from mship.cli.view import spec as _spec

    _status.register(app, get_container)
    _logs.register(app, get_container)
    _diff.register(app, get_container)
    _spec.register(app, get_container)

    parent.add_typer(app, name="view")

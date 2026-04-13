import typer


def register(app: typer.Typer, get_container):
    @app.command()
    def spec():
        """Placeholder; replaced in later task."""
        raise NotImplementedError

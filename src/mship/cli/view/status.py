import typer


def register(app: typer.Typer, get_container):
    @app.command()
    def status():
        """Placeholder; replaced in later task."""
        raise NotImplementedError

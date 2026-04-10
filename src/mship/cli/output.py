import json
import sys
from typing import Any, TextIO

from rich.console import Console
from rich.table import Table


class Output:
    """TTY-aware output formatting. Rich for terminals, JSON for pipes."""

    def __init__(
        self,
        stream: TextIO | None = None,
        err_stream: TextIO | None = None,
    ) -> None:
        self._stream = stream or sys.stdout
        self._err_stream = err_stream or sys.stderr
        self._console = Console(file=self._stream)
        self._err_console = Console(file=self._err_stream, stderr=True)

    @property
    def is_tty(self) -> bool:
        return hasattr(self._stream, "isatty") and self._stream.isatty()

    def json(self, data: dict[str, Any]) -> None:
        self._stream.write(json.dumps(data, indent=2, default=str) + "\n")

    def warning(self, message: str) -> None:
        if self.is_tty:
            self._console.print(f"[yellow]WARNING:[/yellow] {message}")
        else:
            self._stream.write(f"WARNING: {message}\n")

    def error(self, message: str) -> None:
        if self.is_tty:
            self._err_console.print(f"[red]ERROR:[/red] {message}")
        else:
            self._err_stream.write(f"ERROR: {message}\n")

    def success(self, message: str) -> None:
        if self.is_tty:
            self._console.print(f"[green]{message}[/green]")
        else:
            self._stream.write(f"{message}\n")

    def table(self, title: str, columns: list[str], rows: list[list[str]]) -> None:
        if self.is_tty:
            t = Table(title=title)
            for col in columns:
                t.add_column(col)
            for row in rows:
                t.add_row(*row)
            self._console.print(t)
        else:
            self.json({"title": title, "columns": columns, "rows": rows})

    def print(self, message: str) -> None:
        if self.is_tty:
            self._console.print(message)
        else:
            self._stream.write(f"{message}\n")

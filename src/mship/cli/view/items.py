"""`mship view items` — the workspace's WorkItems picker on the master/detail base
(AC2). Reuses load_workitem_index + the pure items formatters. `enter` on a row
composes with `mship layout focus <id>` (AC6)."""
from __future__ import annotations

import subprocess

import typer

from mship.cli.view._master_detail import ListRow, MasterDetailApp
from mship.core.view.items import items_detail, items_label, items_render_text


def _focus_workitem(item_id: str) -> bool:
    """Seam: fire `mship layout focus <id>` for the selected item; returns True on
    success (exit 0) so the picker can report a real outcome, not a false success."""
    return subprocess.run(["mship", "layout", "focus", item_id], check=False).returncode == 0


class ItemsView(MasterDetailApp):
    def __init__(self, summaries, **kw) -> None:
        super().__init__(**kw)
        self._summaries = list(summaries)

    def list_rows(self) -> list[ListRow]:
        return [ListRow(key=s.id, label=items_label(s), detail=items_detail(s))
                for s in self._summaries]

    def header_line(self) -> str | None:
        return f"WorkItems ({len(self._summaries)})"

    def _do_open_entity(self) -> bool:
        key = self.selected_key()
        if key is None:
            return False
        if _focus_workitem(key):
            self._announce(f"Focusing {key}")
        else:
            self._announce(f"Could not focus {key} (is zellij running?)")
        return True

    def _do_copy(self) -> None:
        key = self.selected_key()
        if key:
            self.copy_to_clipboard(key)
            self._announce(f"Copied {key}")
        else:
            self._announce("Nothing to copy here.")


def register(app: "typer.Typer", get_container):
    @app.command()
    def items():
        """This workspace's WorkItems as a navigable picker: id, title, derived
        phase, and attention. enter focuses the item's zellij tab · y copies id."""
        from mship.cli.view._workitems import load_workitem_index
        from mship.cli.output import Output

        container = get_container()
        summaries = load_workitem_index(container)
        if not Output().is_tty:
            typer.echo(items_render_text(summaries))
            return
        ItemsView(summaries).run()

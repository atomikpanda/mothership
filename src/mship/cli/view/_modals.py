"""Small in-process screens for PR4 actions: a request-changes reason prompt and a
read-only cross-entity detail overlay (opened via push_screen — never a second
mship process)."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static


class RequestChangesModal(ModalScreen[str | None]):
    """Prompt a short reason; dismisses with the reason, or None if cancelled/empty."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, spec_id: str) -> None:
        super().__init__()
        self._spec_id = spec_id

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(f"Request changes on {self._spec_id} — reason:", markup=False),
            Input(placeholder="what needs to change…", id="reason"),
        )

    def on_mount(self) -> None:
        self.query_one("#reason", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class EntityScreen(ModalScreen[None]):
    """Read-only, scrollable overlay rendering a linked entity in-process."""
    BINDINGS = [Binding("escape,q", "close", "Close")]

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self._title = title
        self._text = text

    def compose(self) -> ComposeResult:
        yield VerticalScroll(
            Static(f"◆ {self._title}", markup=False),
            Static(self._text, expand=True, markup=False),
        )

    def action_close(self) -> None:
        self.dismiss(None)

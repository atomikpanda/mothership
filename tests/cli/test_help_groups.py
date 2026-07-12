"""
`mship --help` groups commands into rich_help_panel sections.

Every visible top-level command / sub-app is tagged with a
``rich_help_panel="<Group>"`` so the root help renders grouped panels
instead of one flat Commands list.
"""
from typer.testing import CliRunner

from mship.cli import app

runner = CliRunner()

EXPECTED_PANELS = [
    "Workflow",
    "Work items & specs",
    "Messaging",
    "Inspection",
    "Runtime",
    "Maintenance",
    "Setup",
]


def _help_output() -> str:
    # Force a wide render so panel titles never wrap/truncate under CliRunner.
    result = runner.invoke(app, ["--help"], env={"COLUMNS": "120"})
    assert result.exit_code == 0, result.output
    return result.output


def test_all_seven_panels_present():
    out = _help_output()
    for panel in EXPECTED_PANELS:
        assert panel in out, f"missing panel {panel!r} in --help output"


def test_no_flat_commands_panel():
    """Grouped panels replace the default flat 'Commands' section."""
    out = _help_output()
    assert "─ Commands ─" not in out


def test_sample_commands_land_in_help():
    """A representative command from each group shows up in the grouped help."""
    out = _help_output()
    for cmd in ["spawn", "finish", "item", "spec", "serve", "status", "run", "sync", "init"]:
        assert cmd in out, f"command {cmd!r} missing from --help output"

"""Tests for MOS-184: bare `mship spec draft <id>` invocation."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import typer
from typer.testing import CliRunner

from mship.core.spec_draft import new_spec
from mship.core.spec_store import SpecStore


def _make_store(tmp_path: Path) -> tuple[SpecStore, str]:
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    store = SpecStore(specs_dir)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    spec = new_spec("My Feature", now=now)
    store.save(spec)
    return store, spec.id


def _app(tmp_path: Path) -> typer.Typer:
    from mship.cli.spec import register

    class FakeContainer:
        def config_path(self):
            return str(tmp_path / "mothership.yaml")

    app = typer.Typer()
    register(app, lambda: FakeContainer())
    return app


def test_draft_bare_emits_prompt_without_options(tmp_path):
    """Bare `spec draft <id>` (no --from-text / --from-file) must succeed and emit a prompt."""
    _make_store(tmp_path)
    app = _app(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["spec", "draft", "my-feature"])
    assert result.exit_code == 0, result.output
    assert "my-feature" in result.output
    assert "acceptance_criteria" in result.output  # JSON schema shape present


def test_draft_bare_prompt_contains_placeholder_hint(tmp_path):
    """The emitted prompt should guide the user to supply intent."""
    _make_store(tmp_path)
    app = _app(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["spec", "draft", "my-feature"])
    assert result.exit_code == 0
    lower = result.output.lower()
    assert "intent" in lower or "--from-text" in lower or "describe" in lower


def test_draft_both_options_errors(tmp_path):
    """Supplying both --from-text and --from-file is an error."""
    _make_store(tmp_path)
    intent_file = tmp_path / "intent.txt"
    intent_file.write_text("some intent")
    app = _app(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app, ["spec", "draft", "my-feature", "--from-text", "x", "--from-file", str(intent_file)]
    )
    assert result.exit_code != 0
    output_lower = result.output.lower()
    assert any(k in output_lower for k in ("one of", "mutually exclusive", "both", "only one"))


def test_draft_from_text_still_works(tmp_path):
    """--from-text still embeds the provided intent in the prompt."""
    _make_store(tmp_path)
    app = _app(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["spec", "draft", "my-feature", "--from-text", "build a rocket"])
    assert result.exit_code == 0, result.output
    assert "build a rocket" in result.output

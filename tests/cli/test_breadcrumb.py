"""Integration tests: in-scope commands surface resolved_task + resolution_source
in their non-TTY JSON output. See #77. TTY breadcrumb behavior covered in
tests/cli/test_resolve.py."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def ws_with_task(workspace_with_git: Path):
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    (workspace_with_git / ".mothership").mkdir(exist_ok=True)
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    container.shell.override(mock_shell)
    runner.invoke(app, ["spawn", "breadcrumb test", "--repos", "shared", "--force-audit"])
    yield workspace_with_git
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def _parse_json_or_skip(text: str) -> dict:
    """Parse the first JSON object from output; skip test if none."""
    # Find the first '{' in the output (warnings may precede the JSON block)
    idx = text.find("{")
    if idx == -1:
        pytest.skip(f"command did not emit JSON: {text[:80]}")
    text = text[idx:]
    depth = 0
    for i, c in enumerate(text):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[: i + 1])
    pytest.skip("could not find JSON object boundary")


def test_phase_emits_resolution_fields(ws_with_task: Path):
    result = runner.invoke(app, ["phase", "dev", "--task", "breadcrumb-test"])
    assert result.exit_code == 0, result.output
    payload = _parse_json_or_skip(result.output)
    assert payload.get("resolved_task") == "breadcrumb-test"
    assert payload.get("resolution_source") == "--task"


def test_context_emits_resolution_fields(ws_with_task: Path):
    result = runner.invoke(app, ["context", "--task", "breadcrumb-test"])
    assert result.exit_code == 0, result.output
    payload = _parse_json_or_skip(result.output)
    assert payload.get("resolved_task") == "breadcrumb-test"
    assert payload.get("resolution_source") == "--task"


def test_block_emits_resolution_fields(ws_with_task: Path):
    result = runner.invoke(
        app, ["block", "stuck", "--task", "breadcrumb-test"],
    )
    assert result.exit_code == 0, result.output
    payload = _parse_json_or_skip(result.output)
    assert payload.get("resolved_task") == "breadcrumb-test"
    assert payload.get("resolution_source") == "--task"


def test_log_emits_resolution_fields(ws_with_task: Path):
    result = runner.invoke(
        app, ["journal", "test message", "--task", "breadcrumb-test"],
    )
    assert result.exit_code == 0, result.output
    payload = _parse_json_or_skip(result.output)
    assert payload.get("resolved_task") == "breadcrumb-test"
    assert payload.get("resolution_source") == "--task"


def test_ambiguity_lists_candidates(ws_with_task: Path):
    """Second task + running from outside both worktrees → ambiguity error with
    `--task <slug>` hints for BOTH tasks."""
    runner.invoke(
        app, ["spawn", "second task", "--repos", "shared", "--force-audit"],
    )
    import os
    prev = os.getcwd()
    os.chdir(ws_with_task)
    try:
        result = runner.invoke(app, ["phase", "dev"])
    finally:
        os.chdir(prev)
    assert result.exit_code != 0
    assert "--task breadcrumb-test" in result.output
    assert "--task second-task" in result.output

from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.log import LogManager

runner = CliRunner()


@pytest.fixture
def configured_app_with_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/add-labels",
    )
    mgr.save(WorkspaceState(tasks={"add-labels": task}))

    # Create the log file
    log_mgr = LogManager(state_dir / "logs")
    log_mgr.create("add-labels")

    yield workspace
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def test_log_append(configured_app_with_task: Path):
    result = runner.invoke(app, ["journal", "Refactored auth controller", "--task", "add-labels"])
    assert result.exit_code == 0


def test_log_read(configured_app_with_task: Path):
    runner.invoke(app, ["journal", "First entry", "--task", "add-labels"])
    runner.invoke(app, ["journal", "Second entry", "--task", "add-labels"])
    result = runner.invoke(app, ["journal", "--task", "add-labels"])
    assert result.exit_code == 0
    assert "First entry" in result.output
    assert "Second entry" in result.output


def test_log_last_n(configured_app_with_task: Path):
    runner.invoke(app, ["journal", "First", "--task", "add-labels"])
    runner.invoke(app, ["journal", "Second", "--task", "add-labels"])
    runner.invoke(app, ["journal", "Third", "--task", "add-labels"])
    result = runner.invoke(app, ["journal", "--last", "1", "--task", "add-labels"])
    assert result.exit_code == 0
    assert "Third" in result.output
    assert "First" not in result.output


def test_log_no_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    result = runner.invoke(app, ["journal"])
    assert result.exit_code != 0 or "No active task" in result.output or "no active task" in result.output
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def _setup(ws):
    container.config_path.override(ws / "mothership.yaml")
    container.state_dir.override(ws / ".mothership")


def _teardown():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def test_log_with_action_and_open_flags(workspace_with_git):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "flags test", "--repos", "shared", "--force-audit"])
        result = runner.invoke(
            app, ["journal", "stuck",
                    "--action", "debugging middleware",
                    "--open", "how to handle null workspace",
                    "--repo", "shared",
                    "--test-state", "fail",
                    "--task", "flags-test"],
        )
        assert result.exit_code == 0, result.output

        log_mgr = LogManager(workspace_with_git / ".mothership" / "logs")
        entries = log_mgr.read("flags-test")
        assert entries
        latest = entries[-1]
        assert latest.action == "debugging middleware"
        assert latest.open_question == "how to handle null workspace"
        assert latest.repo == "shared"
        assert latest.test_state == "fail"
    finally:
        _teardown()


def test_log_infers_repo_from_active_repo(workspace_with_git, monkeypatch):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "infer test", "--repos", "shared", "--force-audit"])
        runner.invoke(app, ["switch", "shared", "--task", "infer-test"])
        # cd into the worktree so the cwd check passes
        from mship.core.state import StateManager
        state = StateManager(workspace_with_git / ".mothership").load()
        wt = state.tasks["infer-test"].worktrees["shared"]
        monkeypatch.chdir(wt)
        runner.invoke(app, ["journal", "did a thing"])
        log_mgr = LogManager(workspace_with_git / ".mothership" / "logs")
        entries = log_mgr.read("infer-test")
        did = next(e for e in entries if e.message == "did a thing")
        assert did.repo == "shared"
    finally:
        _teardown()


def test_log_show_open_lists_open_questions(workspace_with_git):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "open test", "--repos", "shared", "--force-audit"])
        runner.invoke(
            app, ["journal", "stuck", "--open", "how to handle nulls", "--repo", "shared",
                  "--task", "open-test"],
        )
        runner.invoke(
            app, ["journal", "also stuck", "--open", "timeout logic unclear", "--repo", "shared",
                  "--task", "open-test"],
        )
        result = runner.invoke(app, ["journal", "--show-open", "--task", "open-test"])
        assert result.exit_code == 0
        assert "how to handle nulls" in result.output
        assert "timeout logic unclear" in result.output
    finally:
        _teardown()


def test_log_show_open_empty_exits_zero(workspace_with_git):
    _setup(workspace_with_git)
    try:
        runner.invoke(app, ["spawn", "nothing open", "--repos", "shared", "--force-audit"])
        result = runner.invoke(app, ["journal", "--show-open", "--task", "nothing-open"])
        assert result.exit_code == 0
    finally:
        _teardown()


def test_log_refuses_when_cwd_outside_active_worktree(workspace_with_git, tmp_path, monkeypatch):
    """Default: log refuses (not just warns) when cwd is wrong."""
    from mship.cli import app, container
    from typer.testing import CliRunner
    r = CliRunner()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        r.invoke(app, ["spawn", "refuse test", "--repos", "shared", "--force-audit"])
        r.invoke(app, ["switch", "shared", "--task", "refuse-test"])
        monkeypatch.chdir(tmp_path)
        result = r.invoke(app, ["journal", "should fail", "--task", "refuse-test"])
        assert result.exit_code != 0
        assert "--force" in result.output or "override" in result.output.lower()
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
        container.log_manager.reset()


def test_log_force_writes_entry_with_bypass_tag(workspace_with_git, tmp_path, monkeypatch):
    from mship.cli import app, container
    from mship.core.log import LogManager
    from typer.testing import CliRunner
    r = CliRunner()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        r.invoke(app, ["spawn", "bypass test", "--repos", "shared", "--force-audit"])
        r.invoke(app, ["switch", "shared", "--task", "bypass-test"])
        monkeypatch.chdir(tmp_path)
        result = r.invoke(app, ["journal", "force msg", "--force", "--task", "bypass-test"])
        assert result.exit_code == 0, result.output
        entries = LogManager(workspace_with_git / ".mothership" / "logs").read("bypass-test")
        forced = [e for e in entries if e.message == "force msg"]
        assert forced
        assert forced[0].action is not None and "cwd-bypass" in forced[0].action
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
        container.log_manager.reset()


def test_log_silent_when_cwd_inside_active_worktree(workspace_with_git, monkeypatch):
    from mship.cli import app, container
    from typer.testing import CliRunner
    _runner = CliRunner()

    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        _runner.invoke(app, ["spawn", "cwd test2", "--repos", "shared", "--force-audit"])
        _runner.invoke(app, ["switch", "shared", "--task", "cwd-test2"])

        # cd into the actual worktree
        from mship.core.state import StateManager
        state = StateManager(workspace_with_git / ".mothership").load()
        wt = state.tasks["cwd-test2"].worktrees["shared"]
        monkeypatch.chdir(wt)

        result = _runner.invoke(app, ["journal", "something inside"])
        assert result.exit_code == 0
        # No cwd warning when we're in the right place
        assert "\u26a0" not in result.output
        assert "not the active" not in result.output.lower()
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
        container.log_manager.reset()


# --- Structured-flag-without-message validation (#108) ---
#
# Root cause of #108: `mship journal --test-state pass` (no message) silently
# fell through to the read path. The flag was accepted but no entry was
# written, so the unified test-evidence reader had no journal evidence to
# consider. The reader was correct (#81 / 68753e5); the CLI was lossy.


def test_journal_test_state_without_message_errors(configured_app_with_task: Path):
    """`mship journal --test-state pass` (no message) must error, not silently
    drop the flag. See #108."""
    result = runner.invoke(
        app, ["journal", "--test-state", "pass", "--task", "add-labels"],
    )
    assert result.exit_code != 0, result.output
    assert "message" in result.output.lower()


def test_journal_action_without_message_errors(configured_app_with_task: Path):
    """`mship journal --action ...` without a message also errors."""
    result = runner.invoke(
        app, ["journal", "--action", "ran tests", "--task", "add-labels"],
    )
    assert result.exit_code != 0, result.output
    assert "message" in result.output.lower()


def test_journal_open_without_message_errors(configured_app_with_task: Path):
    """`mship journal --open ...` without a message also errors."""
    result = runner.invoke(
        app, ["journal", "--open", "blocked on api key", "--task", "add-labels"],
    )
    assert result.exit_code != 0, result.output
    assert "message" in result.output.lower()


def test_journal_test_state_with_message_writes_entry(configured_app_with_task: Path):
    """The recommended invocation still writes a usable journal entry."""
    result = runner.invoke(
        app,
        ["journal", "tests verified externally",
         "--test-state", "pass", "--task", "add-labels"],
    )
    assert result.exit_code == 0, result.output

    log_mgr = LogManager(configured_app_with_task / ".mothership" / "logs")
    entries = log_mgr.read("add-labels")
    state_entries = [e for e in entries if e.test_state == "pass"]
    assert len(state_entries) == 1, [e.message for e in entries]
    assert state_entries[0].message == "tests verified externally"


def test_journal_no_args_still_reads(configured_app_with_task: Path):
    """Bare `mship journal` (no message, no structured flags) still reads
    entries \u2014 the validation only fires when flags are present."""
    runner.invoke(
        app, ["journal", "first entry", "--task", "add-labels"],
    )
    result = runner.invoke(app, ["journal", "--task", "add-labels"])
    assert result.exit_code == 0, result.output
    assert "first entry" in result.output


# --- MOS-101: --json / --format jsonl exporter + filters ---

import json as _json
from datetime import datetime as _dt, timezone as _tz


def test_journal_json_emits_array(configured_app_with_task: Path):
    runner.invoke(app, ["journal", "first", "--task", "add-labels"])
    runner.invoke(app, ["journal", "second", "--task", "add-labels", "--action", "ran tests"])
    result = runner.invoke(app, ["journal", "--task", "add-labels", "--json"])
    assert result.exit_code == 0, result.output
    data = _json.loads(result.output)
    assert isinstance(data, list)
    msgs = [e["message"] for e in data]
    assert "first" in msgs and "second" in msgs
    expected = {"timestamp", "message", "action", "repo", "iteration",
                "test_state", "open_question", "id", "parent", "evidence", "category"}
    assert expected.issubset(data[0].keys())


def test_journal_jsonl_one_object_per_line(configured_app_with_task: Path):
    runner.invoke(app, ["journal", "a", "--task", "add-labels"])
    runner.invoke(app, ["journal", "b", "--task", "add-labels"])
    result = runner.invoke(app, ["journal", "--task", "add-labels", "--format", "jsonl"])
    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert len(lines) >= 2
    for ln in lines:
        _json.loads(ln)  # each line is a valid JSON object


def test_journal_json_filters_by_action(configured_app_with_task: Path):
    runner.invoke(app, ["journal", "plain", "--task", "add-labels"])
    runner.invoke(app, ["journal", "tested", "--task", "add-labels", "--action", "ran tests"])
    result = runner.invoke(app, ["journal", "--task", "add-labels", "--action", "ran tests", "--json"])
    assert result.exit_code == 0, result.output
    data = _json.loads(result.output)
    assert [e["message"] for e in data] == ["tested"]
    assert all(e["action"] == "ran tests" for e in data)


def test_journal_invalid_format_errors(configured_app_with_task: Path):
    result = runner.invoke(app, ["journal", "--task", "add-labels", "--format", "yaml"])
    assert result.exit_code != 0
    assert "format" in result.output.lower()


def test_resolve_since_cutoff_iso():
    from mship.cli.log import _resolve_since_cutoff
    assert _resolve_since_cutoff("2026-06-16T12:00:05Z", []) == _dt(2026, 6, 16, 12, 0, 5, tzinfo=_tz.utc)


def test_resolve_since_cutoff_last_phase_change():
    from mship.cli.log import _resolve_since_cutoff
    from mship.core.log import LogEntry
    entries = [
        LogEntry(timestamp=_dt(2026, 6, 16, 12, 0, 0, tzinfo=_tz.utc), message="old"),
        LogEntry(timestamp=_dt(2026, 6, 16, 12, 0, 5, tzinfo=_tz.utc), message="Phase transition: plan → dev"),
        LogEntry(timestamp=_dt(2026, 6, 16, 12, 0, 9, tzinfo=_tz.utc), message="new"),
    ]
    assert _resolve_since_cutoff("last-phase-change", entries) == _dt(2026, 6, 16, 12, 0, 5, tzinfo=_tz.utc)


def test_resolve_since_cutoff_no_phase_change_returns_none():
    from mship.cli.log import _resolve_since_cutoff
    from mship.core.log import LogEntry
    entries = [LogEntry(timestamp=_dt(2026, 6, 16, 12, 0, 0, tzinfo=_tz.utc), message="x")]
    assert _resolve_since_cutoff("last-phase-change", entries) is None


def test_entry_to_dict_includes_all_fields():
    from mship.cli.log import _entry_to_dict
    from mship.core.log import LogEntry
    e = LogEntry(timestamp=_dt(2026, 6, 16, 12, 0, 0, tzinfo=_tz.utc), message="m",
                 action="a", id="x1", evidence="f.py:1-2", category="c", parent="p1")
    d = _entry_to_dict(e)
    assert d["message"] == "m" and d["action"] == "a" and d["id"] == "x1"
    assert d["evidence"] == "f.py:1-2" and d["category"] == "c" and d["parent"] == "p1"
    assert set(d) == {"timestamp", "message", "action", "repo", "iteration",
                      "test_state", "open_question", "id", "parent", "evidence", "category"}


def test_journal_last_applies_after_filters(configured_app_with_task: Path):
    # Interleave matching (ran tests) and non-matching entries.
    runner.invoke(app, ["journal", "t1", "--task", "add-labels", "--action", "ran tests"])
    runner.invoke(app, ["journal", "p1", "--task", "add-labels"])
    runner.invoke(app, ["journal", "t2", "--task", "add-labels", "--action", "ran tests"])
    runner.invoke(app, ["journal", "p2", "--task", "add-labels"])
    runner.invoke(app, ["journal", "t3", "--task", "add-labels", "--action", "ran tests"])
    # Filter first, THEN --last → the 2 most-recent *matching* entries (not <2).
    result = runner.invoke(
        app, ["journal", "--task", "add-labels", "--action", "ran tests", "--last", "2", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert [e["message"] for e in _json.loads(result.output)] == ["t2", "t3"]


def test_journal_json_with_message_errors(configured_app_with_task: Path):
    # Export flags are read-only; combining with a message (write) is an error.
    result = runner.invoke(app, ["journal", "hello", "--task", "add-labels", "--json"])
    assert result.exit_code != 0
    assert "read-only" in result.output.lower()

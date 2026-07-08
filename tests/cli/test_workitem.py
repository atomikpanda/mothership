import json
import subprocess
from datetime import datetime, timezone

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.run_state import RunStateRepo
from mship.core.spec import Spec
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager, Task

runner = CliRunner()

_NOW = datetime(2026, 7, 8, tzinfo=timezone.utc)


def _isolate(tmp_path):
    """Point the global container at a throwaway workspace."""
    (tmp_path / "mothership.yaml").write_text("workspace: testws\nrepos: {}\n")
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    container.config.reset()  # drop any singleton cached by another test
    # state_manager/log_manager are Singletons keyed off state_dir; drop any
    # instance a prior test bound to its own tmp so they rebind to this one.
    container.state_manager.reset()
    container.log_manager.reset()


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def _make_origin(tmp_path):
    """A bare repo the run-state ref can push to, wired as the workspace origin."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=tmp_path,
                   check=True, capture_output=True, text=True)
    return origin


def _eligible_item(tmp_path, spec_id="s-1"):
    """Create an unattended, phase=ready item bound to an approved spec; return its id."""
    res = runner.invoke(app, ["item", "new", "Do the thing", "--kind", "feature"])
    assert res.exit_code == 0, res.output
    item_id = res.output.strip()
    SpecStore(tmp_path / "specs").save(Spec(
        id=spec_id, title="Do the thing", status="approved",
        created_at=_NOW, updated_at=_NOW, body="## Problem\n\nNeeds doing.\n",
    ))
    assert runner.invoke(app, ["item", "link-spec", item_id, spec_id]).exit_code == 0
    assert runner.invoke(app, ["item", "phase", item_id, "ready"]).exit_code == 0
    assert runner.invoke(app, ["item", "unattended", item_id, "--on"]).exit_code == 0
    return item_id


def test_item_run_next_emits_prompt_and_claims(tmp_path):
    _isolate(tmp_path)
    origin = _make_origin(tmp_path)
    try:
        item_id = _eligible_item(tmp_path)
        res = runner.invoke(app, ["--json", "item", "run-next"])
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["runnable"] is True
        assert payload["item_id"] == item_id
        assert "Do the thing" in payload["prompt"]
        # the claim is recorded on the shared run-state ref (readable by any run)
        assert RunStateRepo(origin, tmp_path / "verify").read_claim(item_id) is not None
    finally:
        _reset()


def test_item_run_next_noop_exit_zero_when_empty(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["--json", "item", "run-next"])
        assert res.exit_code == 0, res.output
        assert json.loads(res.output) == {"runnable": False}
    finally:
        _reset()


def test_item_bail_logs_reason_and_releases(tmp_path):
    _isolate(tmp_path)
    origin = _make_origin(tmp_path)
    try:
        item_id = _eligible_item(tmp_path)
        # Give the item a task so the derived "blocked" flag is assertable
        # (blocked is computed from a task's blocked_reason).
        sm = StateManager(tmp_path / ".mothership")
        sm.mutate(lambda s: s.tasks.__setitem__("t-1", Task(
            slug="t-1", description="d", phase="dev", created_at=_NOW,
            affected_repos=["mothership"], branch="feat/t-1")))
        assert runner.invoke(app, ["item", "link-task", item_id, "t-1"]).exit_code == 0

        # Claim it via run-next; same process => same holder, so bail can release.
        assert runner.invoke(app, ["--json", "item", "run-next"]).exit_code == 0

        res = runner.invoke(app, ["item", "bail", item_id, "--reason", "fork on auth"])
        assert res.exit_code == 0, res.output

        rs = RunStateRepo(origin, tmp_path / "verify")
        assert any("fork on auth" in e.text for e in rs.read_log(item_id))  # reason logged
        assert rs.read_claim(item_id) is None                              # claim released
        assert sm.load().tasks["t-1"].blocked_reason == "fork on auth"     # item blocked
    finally:
        _reset()


def test_new_then_list_roundtrip(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Make capture conversational", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        res = runner.invoke(app, ["--json", "item", "list"])
        assert res.exit_code == 0, res.output
        rows = json.loads(res.output)
        assert len(rows) == 1
        assert rows[0]["title"] == "Make capture conversational"
        assert rows[0]["phase"] == "inbox"
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_new_with_invalid_kind_errors_cleanly(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "bogus"])
        assert res.exit_code == 1
        assert "invalid kind" in res.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_phase_with_invalid_value_errors_cleanly(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        item_id = res.output.strip()
        res = runner.invoke(app, ["item", "phase", item_id, "bogus"])
        assert res.exit_code == 1
        assert "invalid phase" in res.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_item_unattended_cli(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        item_id = res.output.strip()

        res = runner.invoke(app, ["item", "unattended", item_id, "--on"])
        assert res.exit_code == 0, res.output
        assert "unattended=True" in res.output

        res = runner.invoke(app, ["item", "show", item_id])
        assert res.exit_code == 0, res.output
        assert json.loads(res.output)["unattended"] is True

        res = runner.invoke(app, ["item", "unattended", item_id, "--off"])
        assert res.exit_code == 0, res.output
        assert "unattended=False" in res.output

        res = runner.invoke(app, ["item", "show", item_id])
        assert res.exit_code == 0, res.output
        assert json.loads(res.output)["unattended"] is False
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_link_url_with_invalid_provider_errors_cleanly(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        item_id = res.output.strip()
        res = runner.invoke(app, ["item", "link-url", item_id, "https://x", "--provider", "slack"])
        assert res.exit_code == 1
        assert "invalid provider" in res.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
